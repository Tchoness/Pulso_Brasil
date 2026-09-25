import json
import logging
import os
import time
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from coletor_bcb import gerar_documento, registrar_resumo


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CHAVE_PADRAO = "data/data.json"
CACHE_CONTROL_PADRAO = "public, max-age=300, must-revalidate"
GITHUB_API_VERSION = "2026-03-10"
STATUS_GITHUB_ACEITOS = {200, 204}


class DocumentoInvalidoError(RuntimeError):
    """Impede que uma coleta inválida substitua o JSON publicado."""


def obter_variavel_obrigatoria(nome: str) -> str:
    """Lê uma variável de ambiente e falha com uma mensagem clara."""

    valor = os.getenv(nome, "").strip()

    if not valor:
        raise RuntimeError(
            f"A variável de ambiente {nome} não foi configurada."
        )

    return valor


def obter_booleano(nome: str, padrao: bool = False) -> bool:
    """Converte uma variável de ambiente para booleano."""

    valor_padrao = "true" if padrao else "false"
    valor = os.getenv(nome, valor_padrao).strip().lower()

    return valor in {"1", "true", "sim", "yes", "on"}


def validar_documento(
    documento: dict[str, Any],
    publicar_com_erros: bool,
) -> None:
    """Valida a coleta antes de substituir o objeto existente no S3."""

    total_indicadores = documento.get("total_indicadores", 0)
    indicadores = documento.get("indicadores", [])
    erros = documento.get("erros", [])

    if not isinstance(indicadores, list):
        raise DocumentoInvalidoError(
            "O campo indicadores não contém uma lista."
        )

    if total_indicadores != len(indicadores):
        raise DocumentoInvalidoError(
            "O total de indicadores não corresponde aos dados coletados."
        )

    if total_indicadores == 0:
        raise DocumentoInvalidoError(
            "A coleta não retornou nenhum indicador."
        )

    if erros and not publicar_com_erros:
        raise DocumentoInvalidoError(
            "A coleta apresentou erros e o JSON anterior será preservado."
        )


def serializar_documento(documento: dict[str, Any]) -> bytes:
    """Converte o documento para JSON UTF-8 pronto para envio ao S3."""

    conteudo = json.dumps(
        documento,
        ensure_ascii=False,
        indent=2,
    )

    return conteudo.encode("utf-8")


def publicar_no_s3(
    documento: dict[str, Any],
    nome_bucket: str,
    chave_objeto: str,
) -> str:
    """Publica o documento no S3 e devolve o ETag criado."""

    cliente_s3 = boto3.client("s3")
    corpo = serializar_documento(documento)

    try:
        resposta = cliente_s3.put_object(
            Bucket=nome_bucket,
            Key=chave_objeto,
            Body=corpo,
            ContentType="application/json; charset=utf-8",
            CacheControl=os.getenv(
                "CACHE_CONTROL",
                CACHE_CONTROL_PADRAO,
            ),
            ServerSideEncryption="AES256",
            Metadata={
                "gerado-em": documento["gerado_em"],
            },
        )
    except (BotoCoreError, ClientError) as erro:
        logger.exception(
            "Não foi possível publicar s3://%s/%s.",
            nome_bucket,
            chave_objeto,
        )
        raise RuntimeError(
            "Falha ao publicar o documento no S3."
        ) from erro

    etag = resposta.get("ETag", "").strip('"')

    logger.info(
        "Documento publicado em s3://%s/%s (%s bytes).",
        nome_bucket,
        chave_objeto,
        len(corpo),
    )

    return etag


def montar_itens_historico(
    documento: dict[str, Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """Cria os itens enxutos que representam uma coleta no DynamoDB."""

    coleta_em = documento["gerado_em"]
    itens: list[dict[str, Any]] = []

    for indicador in documento.get("indicadores", []):
        item = {
            "pk": f"INDICADOR#{indicador['id']}",
            "sk": coleta_em,
            "tipo": "indicador",
            "indicador_id": indicador["id"],
            "nome": indicador["nome"],
            "valor": Decimal(str(indicador["ultimo_valor"])),
            "unidade": indicador["unidade"],
            "data_referencia": indicador["data_ultimo_valor"],
            "periodicidade": indicador["periodicidade"],
            "fonte": indicador["fonte"],
            "coleta_em": coleta_em,
            "request_id": request_id,
        }

        periodo_referencia = indicador.get("periodo_ultimo_valor")

        if periodo_referencia:
            item["periodo_referencia"] = periodo_referencia

        itens.append(item)

    itens.append(
        {
            "pk": "COLETA",
            "sk": coleta_em,
            "tipo": "resumo_coleta",
            "coleta_em": coleta_em,
            "request_id": request_id,
            "total_indicadores": documento["total_indicadores"],
            "total_erros": len(documento.get("erros", [])),
            "status": (
                "sucesso"
                if not documento.get("erros")
                else "sucesso_com_erros"
            ),
        }
    )

    return itens


def gravar_historico_dynamodb(
    documento: dict[str, Any],
    nome_tabela: str,
    request_id: str,
) -> int:
    """Grava o resumo e os valores atuais sem bloquear o fluxo do S3."""

    tabela = boto3.resource("dynamodb").Table(nome_tabela)
    itens = montar_itens_historico(documento, request_id)

    try:
        with tabela.batch_writer() as lote:
            for item in itens:
                lote.put_item(Item=item)
    except (BotoCoreError, ClientError) as erro:
        logger.exception(
            "Não foi possível gravar o histórico na tabela %s.",
            nome_tabela,
        )
        raise RuntimeError(
            "Falha ao gravar o histórico no DynamoDB."
        ) from erro

    logger.info(
        "%s item(ns) gravado(s) na tabela DynamoDB %s.",
        len(itens),
        nome_tabela,
    )

    return len(itens)


def obter_token_github(nome_parametro: str) -> str:
    """Obtém o token criptografado no Parameter Store."""

    cliente_ssm = boto3.client("ssm")

    try:
        resposta = cliente_ssm.get_parameter(
            Name=nome_parametro,
            WithDecryption=True,
        )
    except (BotoCoreError, ClientError) as erro:
        logger.exception(
            "Não foi possível obter o token do GitHub no Parameter Store."
        )
        raise RuntimeError(
            "Falha ao obter o token do GitHub."
        ) from erro

    token = resposta.get("Parameter", {}).get("Value", "").strip()

    if not token:
        raise RuntimeError(
            "O parâmetro do token do GitHub está vazio."
        )

    return token


def disparar_workflow_github(
    token: str,
    repositorio: str,
    arquivo_workflow: str,
    referencia: str,
    tentativas: int = 3,
) -> dict[str, Any]:
    """Dispara o workflow do GitHub Pages após publicar o JSON."""

    url = (
        f"https://api.github.com/repos/{repositorio}/actions/"
        f"workflows/{arquivo_workflow}/dispatches"
    )
    corpo = json.dumps({"ref": referencia}).encode("utf-8")
    ultimo_erro: Exception | None = None

    for tentativa in range(1, tentativas + 1):
        requisicao = Request(
            url=url,
            data=corpo,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "PulsoBrasil-Lambda/1.0",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )

        try:
            with urlopen(requisicao, timeout=15) as resposta:
                status = resposta.status
                conteudo = resposta.read().decode("utf-8")

                if status not in STATUS_GITHUB_ACEITOS:
                    raise RuntimeError(
                        f"O GitHub respondeu com status {status}."
                    )

                dados_resposta = {}

                if conteudo.strip():
                    try:
                        dados_resposta = json.loads(conteudo)
                    except json.JSONDecodeError:
                        logger.warning(
                            "GitHub aceitou o disparo, mas retornou "
                            "um corpo não reconhecido."
                        )

                logger.info(
                    "Workflow %s disparado no GitHub. status=%s",
                    arquivo_workflow,
                    status,
                )

                return {
                    "status_code": status,
                    "workflow_run_id": dados_resposta.get(
                        "workflow_run_id"
                    ),
                    "workflow_run_url": dados_resposta.get("html_url"),
                }

        except HTTPError as erro:
            ultimo_erro = erro
            erro_temporario = erro.code in {
                408,
                429,
                500,
                502,
                503,
                504,
            }

            if not erro_temporario or tentativa == tentativas:
                logger.exception(
                    "GitHub recusou o disparo do workflow. status=%s",
                    erro.code,
                )
                break

        except (URLError, TimeoutError, RuntimeError) as erro:
            ultimo_erro = erro

            if tentativa == tentativas:
                logger.exception(
                    "Falha de comunicação ao disparar o workflow do GitHub."
                )
                break

        espera = 2 ** (tentativa - 1)
        logger.warning(
            "Nova tentativa de disparo do GitHub em %s segundo(s).",
            espera,
        )
        time.sleep(espera)

    raise RuntimeError(
        "Não foi possível disparar o workflow do GitHub."
    ) from ultimo_erro


def lambda_handler(event: dict, context: Any) -> dict[str, Any]:
    """Ponto de entrada executado pela AWS Lambda."""

    nome_bucket = obter_variavel_obrigatoria("BUCKET_NAME")
    nome_tabela = obter_variavel_obrigatoria("HISTORY_TABLE_NAME")
    nome_parametro_token = obter_variavel_obrigatoria(
        "GITHUB_TOKEN_PARAMETER_NAME"
    )
    repositorio_github = obter_variavel_obrigatoria(
        "GITHUB_REPOSITORY"
    )
    arquivo_workflow = obter_variavel_obrigatoria(
        "GITHUB_WORKFLOW_FILE"
    )
    referencia_workflow = obter_variavel_obrigatoria(
        "GITHUB_WORKFLOW_REF"
    )
    chave_objeto = os.getenv("OBJECT_KEY", CHAVE_PADRAO).strip()
    publicar_com_erros = obter_booleano("PUBLICAR_COM_ERROS")

    if not chave_objeto:
        raise RuntimeError(
            "A variável OBJECT_KEY não pode estar vazia."
        )

    request_id = getattr(context, "aws_request_id", "execucao-local")

    logger.info(
        "Iniciando coleta. request_id=%s destino=s3://%s/%s",
        request_id,
        nome_bucket,
        chave_objeto,
    )

    documento = gerar_documento()
    registrar_resumo(documento)
    validar_documento(documento, publicar_com_erros)

    etag = publicar_no_s3(
        documento=documento,
        nome_bucket=nome_bucket,
        chave_objeto=chave_objeto,
    )

    historico_gravado = False
    total_itens_historico = 0

    try:
        total_itens_historico = gravar_historico_dynamodb(
            documento=documento,
            nome_tabela=nome_tabela,
            request_id=request_id,
        )
        historico_gravado = True
    except RuntimeError:
        logger.exception(
            "O site foi atualizado no S3, mas o histórico não foi gravado."
        )

    token_github = obter_token_github(nome_parametro_token)
    resultado_github = disparar_workflow_github(
        token=token_github,
        repositorio=repositorio_github,
        arquivo_workflow=arquivo_workflow,
        referencia=referencia_workflow,
    )

    return {
        "status": "sucesso",
        "request_id": request_id,
        "bucket": nome_bucket,
        "object_key": chave_objeto,
        "etag": etag,
        "history_table": nome_tabela,
        "history_saved": historico_gravado,
        "history_items": total_itens_historico,
        "github_dispatch_sent": True,
        "github_status_code": resultado_github["status_code"],
        "github_workflow_run_id": resultado_github["workflow_run_id"],
        "github_workflow_run_url": resultado_github[
            "workflow_run_url"
        ],
        "gerado_em": documento["gerado_em"],
        "total_indicadores": documento["total_indicadores"],
        "total_erros": len(documento["erros"]),
    }
