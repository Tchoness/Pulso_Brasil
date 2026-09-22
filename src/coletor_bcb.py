import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


URL_BASE_BCB = (
    "https://api.bcb.gov.br/dados/serie/"
    "bcdata.sgs.{codigo}/dados"
)

URL_IBGE_DESEMPREGO = (
    "https://servicodados.ibge.gov.br/api/v3/"
    "agregados/6381/periodos/-36/variaveis/4099"
)

PASTA_PROJETO = Path(__file__).resolve().parent.parent
ARQUIVO_SAIDA = PASTA_PROJETO / "data" / "data.json"

TIMEOUT_CONEXAO = 5
TIMEOUT_RESPOSTA = 30


SERIES = {
    "selic_meta": {
        "codigo": 432,
        "nome": "Meta Selic",
        "descricao": "A taxa Selic é a taxa básica de juros da economia brasileira, funcionando como o \"preço\" do dinheiro no país. É o farol que guia todas as outras taxas de juros do Brasil, é definida a cada 45 dias pelo Banco Central através do Copom. Serve para controlar a inflação (aumento dos preços).",
        "unidade": "% ao ano",
        "periodicidade": "diária",
        "dias_historico": 365,
        "fonte": "Banco Central do Brasil",
    },
    "ipca_mensal": {
        "codigo": 433,
        "nome": "IPCA mensal",
        "descricao": "O IPCA mensal é o indicador oficial que mostra quanto os preços das coisas subiram ou caíram no Brasil ao longo de um mês específico. Ele é calculado pelo IBGE e reflete a variação média dos preços de uma cesta de produtos e serviços consumidos pelas famílias brasileiras.",
        "unidade": "% ao mês",
        "periodicidade": "mensal",
        "dias_historico": 1095,
        "fonte": "Banco Central do Brasil / IBGE",
    },
    "dolar_compra": {
        "codigo": 1,
        "nome": "Dólar comercial",
        "descricao": "Pense nele como o preço do dólar no \"atacado\". Ele serve como a base de preço para o comércio internacional e grandes movimentações econômicas, sendo bem diferente do dólar turismo, que é o que você compra em casas de câmbio para viajar.",
        "unidade": "R$ por US$",
        "periodicidade": "diária",
        "dias_historico": 365,
        "fonte": "Banco Central do Brasil",
    },
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def criar_sessao_http() -> requests.Session:
    """
    Cria uma sessão HTTP com tentativas automáticas em caso de
    erros temporários da API.
    """

    politica_retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adaptador = HTTPAdapter(max_retries=politica_retry)

    sessao = requests.Session()
    sessao.mount("https://", adaptador)
    sessao.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "PulsoBrasil/1.0",
        }
    )

    return sessao


def converter_data(data_bcb: str) -> str:
    """
    Converte uma data no formato DD/MM/AAAA para AAAA-MM-DD.
    """

    data_convertida = datetime.strptime(
        data_bcb,
        "%d/%m/%Y",
    ).date()

    return data_convertida.isoformat()


def converter_valor(valor_bcb: str) -> float:
    """
    Converte o valor retornado pela API para número decimal.
    """

    valor_normalizado = valor_bcb.strip().replace(",", ".")
    return float(valor_normalizado)


def coletar_serie(
    sessao: requests.Session,
    identificador: str,
    configuracao: dict,
) -> dict:
    """
    Consulta uma série do Banco Central utilizando
    um intervalo de datas.
    """

    codigo = configuracao["codigo"]
    dias_historico = configuracao["dias_historico"]

    data_final = datetime.now(timezone.utc).date()
    data_inicial = data_final - timedelta(days=dias_historico)

    url = URL_BASE_BCB.format(codigo=codigo)

    parametros = {
        "formato": "json",
        "dataInicial": data_inicial.strftime("%d/%m/%Y"),
        "dataFinal": data_final.strftime("%d/%m/%Y"),
    }

    logging.info(
        "Consultando %s - série SGS %s - período de %s até %s",
        configuracao["nome"],
        codigo,
        data_inicial.isoformat(),
        data_final.isoformat(),
    )

    resposta = sessao.get(
        url,
        params=parametros,
        timeout=(TIMEOUT_CONEXAO, TIMEOUT_RESPOSTA),
    )

    resposta.raise_for_status()

    dados_api = resposta.json()

    if not isinstance(dados_api, list):
        raise ValueError(
            f"A série {codigo} retornou um formato inesperado: "
            f"{dados_api}"
        )

    valores = []

    for registro in dados_api:
        if "data" not in registro or "valor" not in registro:
            logging.warning(
                "Registro ignorado na série %s: %s",
                codigo,
                registro,
            )
            continue

        try:
            valores.append(
                {
                    "data": converter_data(registro["data"]),
                    "valor": converter_valor(registro["valor"]),
                }
            )
        except (ValueError, TypeError) as erro:
            logging.warning(
                "Não foi possível converter o registro %s: %s",
                registro,
                erro,
            )

    valores.sort(key=lambda item: item["data"])

    if not valores:
        raise ValueError(
            f"Nenhum valor válido foi encontrado para a série {codigo}."
        )

    return {
        "id": identificador,
        "codigo_sgs": codigo,
        "nome": configuracao["nome"],
        "descricao": configuracao["descricao"],
        "unidade": configuracao["unidade"],
        "periodicidade": configuracao["periodicidade"],
        "fonte": configuracao["fonte"],
        "url_fonte": resposta.url,
        "periodo_consultado": {
            "data_inicial": data_inicial.isoformat(),
            "data_final": data_final.isoformat(),
        },
        "ultimo_valor": valores[-1]["valor"],
        "data_ultimo_valor": valores[-1]["data"],
        "total_registros": len(valores),
        "valores": valores,
    }

MESES_ABREVIADOS = [
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
]


def criar_nome_trimestre_movel(codigo_periodo: str) -> str:

    if len(codigo_periodo) != 6:
        return codigo_periodo

    ano = int(codigo_periodo[:4])
    mes_final = int(codigo_periodo[4:6])

    if mes_final < 1 or mes_final > 12:
        return codigo_periodo

    meses = []

    for deslocamento in (2, 1, 0):
        indice_absoluto = (
            ano * 12 +
            mes_final -
            1 -
            deslocamento
        )

        indice_mes = indice_absoluto % 12
        meses.append(MESES_ABREVIADOS[indice_mes])

    return f"{'-'.join(meses)}/{ano}"


def coletar_taxa_desocupacao(
    sessao: requests.Session,
) -> dict:

    logging.info(
        "Consultando Taxa de desocupação - "
        "SIDRA tabela 6381"
    )

    resposta = sessao.get(
        URL_IBGE_DESEMPREGO,
        params={
            "localidades": "N1[all]",
        },
        timeout=(TIMEOUT_CONEXAO, TIMEOUT_RESPOSTA),
    )

    resposta.raise_for_status()

    dados_api = resposta.json()

    if not isinstance(dados_api, list) or not dados_api:
        raise ValueError(
            "O IBGE retornou uma resposta vazia ou inválida."
        )

    variavel = dados_api[0]
    resultados = variavel.get("resultados", [])

    valores_por_data = {}

    for resultado in resultados:
        series = resultado.get("series", [])

        for serie in series:
            localidade = serie.get("localidade", {})
            nome_localidade = localidade.get("nome")

            if nome_localidade != "Brasil":
                continue

            registros = serie.get("serie", {})

            for codigo_periodo, valor_api in registros.items():
                try:
                    if len(codigo_periodo) != 6:
                        continue

                    ano = int(codigo_periodo[:4])
                    mes = int(codigo_periodo[4:6])

                    if mes < 1 or mes > 12:
                        continue

                    valor = converter_valor(valor_api)
                    data_iso = f"{ano}-{mes:02d}-01"

                    valores_por_data[data_iso] = {
                        "data": data_iso,
                        "valor": valor,
                        "periodo": criar_nome_trimestre_movel(
                            codigo_periodo
                        ),
                    }

                except (ValueError, TypeError):
                    logging.warning(
                        "Registro inválido do IBGE ignorado: "
                        "período=%s, valor=%s",
                        codigo_periodo,
                        valor_api,
                    )

    valores = sorted(
        valores_por_data.values(),
        key=lambda registro: registro["data"],
    )

    if not valores:
        raise ValueError(
            "Nenhum valor válido de desemprego foi encontrado."
        )

    ultimo = valores[-1]

    return {
        "id": "taxa_desocupacao",
        "codigo_fonte": 6381,
        "rotulo_codigo": "SIDRA",
        "nome": "Taxa de desocupação",
        "descricao": (
            "Percentual das pessoas de 14 anos ou mais "
            "que estavam desocupadas e procurando trabalho"
        ),
        "unidade": "%",
        "periodicidade": "trimestre móvel",
        "fonte": "IBGE - PNAD Contínua",
        "url_fonte": resposta.url,
        "ultimo_valor": ultimo["valor"],
        "data_ultimo_valor": ultimo["data"],
        "periodo_ultimo_valor": ultimo["periodo"],
        "total_registros": len(valores),
        "valores": valores,
    }

def gerar_documento() -> dict:
    """
    Coleta todas as séries e monta o documento consumido
    pelo frontend.
    """

    sessao = criar_sessao_http()
    indicadores = []
    erros = []

    for identificador, configuracao in SERIES.items():
        try:
            serie = coletar_serie(
                sessao,
                identificador,
                configuracao,
            )

            indicadores.append(serie)

            logging.info(
                "%s coletada: %s registros",
                configuracao["nome"],
                serie["total_registros"],
            )

        except requests.exceptions.Timeout:
            mensagem = (
                f"Timeout ao consultar {configuracao['nome']}."
            )
            logging.error(mensagem)
            erros.append(mensagem)

        except requests.exceptions.ConnectionError as erro:
            mensagem = (
                f"Erro de conexão ao consultar "
                f"{configuracao['nome']}: {erro}"
            )
            logging.error(mensagem)
            erros.append(mensagem)

        except requests.exceptions.HTTPError as erro:
            status = (
                erro.response.status_code
                if erro.response is not None
                else "desconhecido"
            )

            mensagem = (
                f"Erro HTTP {status} ao consultar "
                f"{configuracao['nome']}."
            )
            logging.error(mensagem)
            erros.append(mensagem)

        except (ValueError, KeyError, TypeError) as erro:
            mensagem = (
                f"Erro no processamento de "
                f"{configuracao['nome']}: {erro}"
            )
            logging.error(mensagem)
            erros.append(mensagem)

    try:
        indicador_desemprego = coletar_taxa_desocupacao(
            sessao
        )

        indicadores.append(indicador_desemprego)

        logging.info(
            "Taxa de desocupação coletada: %s registros",
            indicador_desemprego["total_registros"],
        )

    except requests.exceptions.Timeout:
        mensagem = (
            "Timeout ao consultar Taxa de desocupação no IBGE."
        )
        logging.error(mensagem)
        erros.append(mensagem)

    except requests.exceptions.ConnectionError as erro:
        mensagem = (
            "Erro de conexão ao consultar "
            f"Taxa de desocupação no IBGE: {erro}"
        )
        logging.error(mensagem)
        erros.append(mensagem)

    except requests.exceptions.HTTPError as erro:
        status = (
            erro.response.status_code
            if erro.response is not None
            else "desconhecido"
        )

        mensagem = (
            f"Erro HTTP {status} ao consultar "
            "Taxa de desocupação no IBGE."
        )
        logging.error(mensagem)
        erros.append(mensagem)

    except (ValueError, KeyError, TypeError) as erro:
        mensagem = (
            "Erro ao processar Taxa de desocupação "
            f"do IBGE: {erro}"
        )
        logging.error(mensagem)
        erros.append(mensagem)

    documento = {
        "projeto": "Pulso Brasil",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "total_indicadores": len(indicadores),
        "indicadores": indicadores,
        "erros": erros,
    }

    sessao.close()
    return documento

def salvar_json_local(
    documento: dict,
    caminho_saida: Path = ARQUIVO_SAIDA,
) -> Path:
    """
    Salva o documento no disco para desenvolvimento local
    e para o deploy estático atual.
    """

    caminho_saida.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with caminho_saida.open(
        mode="w",
        encoding="utf-8",
    ) as arquivo:
        json.dump(
            documento,
            arquivo,
            ensure_ascii=False,
            indent=2,
        )

    logging.info("Arquivo gerado em: %s", caminho_saida)
    return caminho_saida

def registrar_resumo(documento: dict) -> None:
    """Registra no log o resultado consolidado da coleta."""

    erros = documento.get("erros", [])

    if erros:
        logging.warning(
            "A coleta terminou com %s erro(s).",
            len(erros),
        )
    else:
        logging.info("Coleta finalizada sem erros.")


def gerar_arquivo_json() -> dict:
    """
    Executa o fluxo local completo.

    Esta função mantém compatibilidade com o comando atual,
    enquanto gerar_documento() poderá ser reutilizada pela Lambda.
    """

    documento = gerar_documento()
    salvar_json_local(documento)
    registrar_resumo(documento)
    return documento


if __name__ == "__main__":
    gerar_arquivo_json()