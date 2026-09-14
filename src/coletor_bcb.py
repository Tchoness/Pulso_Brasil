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

PASTA_PROJETO = Path(__file__).resolve().parent.parent
ARQUIVO_SAIDA = PASTA_PROJETO / "data" / "data.json"

TIMEOUT_CONEXAO = 5
TIMEOUT_RESPOSTA = 30


SERIES = {
    "selic_meta": {
        "codigo": 432,
        "nome": "Meta Selic",
        "descricao": "Meta da taxa Selic definida pelo Copom",
        "unidade": "% ao ano",
        "periodicidade": "diária",
        "dias_historico": 365,
        "fonte": "Banco Central do Brasil",
    },
    "ipca_mensal": {
        "codigo": 433,
        "nome": "IPCA mensal",
        "descricao": "Variação mensal do IPCA",
        "unidade": "% ao mês",
        "periodicidade": "mensal",
        "dias_historico": 1095,
        "fonte": "Banco Central do Brasil / IBGE",
    },
    "dolar_compra": {
        "codigo": 1,
        "nome": "Dólar comercial",
        "descricao": "Taxa de câmbio de compra do dólar",
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


def gerar_arquivo_json() -> None:
    """
    Coleta todas as séries e gera o arquivo utilizado futuramente
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

    documento = {
        "projeto": "Pulso Brasil",
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "total_indicadores": len(indicadores),
        "indicadores": indicadores,
        "erros": erros,
    }

    ARQUIVO_SAIDA.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with ARQUIVO_SAIDA.open(
        mode="w",
        encoding="utf-8",
    ) as arquivo:
        json.dump(
            documento,
            arquivo,
            ensure_ascii=False,
            indent=2,
        )

    logging.info("Arquivo gerado em: %s", ARQUIVO_SAIDA)

    if erros:
        logging.warning(
            "A coleta terminou com %s erro(s).",
            len(erros),
        )
    else:
        logging.info("Coleta finalizada sem erros.")


if __name__ == "__main__":
    gerar_arquivo_json()