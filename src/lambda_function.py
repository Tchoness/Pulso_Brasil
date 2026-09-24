import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from coletor_bcb import gerar_documento, registrar_resumo


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CHAVE_PADRAO = "data/data.json"
CACHE_CONTROL_PADRAO = "public, max-age=300, must-revalidate"


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


def lambda_handler(event: dict, context: Any) -> dict[str, Any]:
    """Ponto de entrada executado pela AWS Lambda."""

    nome_bucket = obter_variavel_obrigatoria("BUCKET_NAME")
    nome_tabela = obter_variavel_obrigatoria("HISTORY_TABLE_NAME")
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

    return {
        "status": "sucesso",
        "request_id": request_id,
        "bucket": nome_bucket,
        "object_key": chave_objeto,
        "etag": etag,
        "history_table": nome_tabela,
        "history_saved": historico_gravado,
        "history_items": total_itens_historico,
        "gerado_em": documento["gerado_em"],
        "total_indicadores": documento["total_indicadores"],
        "total_erros": len(documento["erros"]),
    }
