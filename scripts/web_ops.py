#!/usr/bin/env python
"""Operações do site (Fase 6).

Uso:
    web_ops.py deploy-site                 envia web/ e config.js ao bucket e invalida o cache
    web_ops.py publish                     gera agora o data/forecasts.json
    web_ops.py create-user --email E-MAIL  cria um usuário; a AWS envia a senha temporária

A configuração do site (região e ID do cliente do Cognito) é pública por natureza.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT: Final = Path(__file__).resolve().parents[1]
WEB_DIR: Final = ROOT / "web"
REGION: Final = "us-east-2"
STACK: Final = "chuva-bh-web"
# HTML e configuração mudam a cada deploy; CSS e JS recebem cache curto e são invalidados.
CACHE: Final = {".html": "no-cache", ".js": "max-age=300", ".css": "max-age=300"}


def outputs(session: boto3.Session) -> dict[str, str]:
    description = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in description.get("Outputs", [])}


def config_js(client_id: str) -> str:
    config = {"region": REGION, "clientId": client_id}
    return f"window.CHUVA_CONFIG = {json.dumps(config)};\n"


def deploy_site(session: boto3.Session) -> str:
    out = outputs(session)
    s3 = session.client("s3")
    files = {path.name: path.read_bytes() for path in sorted(WEB_DIR.glob("*")) if path.is_file()}
    files["config.js"] = config_js(out["UserPoolClientId"]).encode("utf-8")
    for name, body in files.items():
        suffix = Path(name).suffix
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        s3.put_object(
            Bucket=out["SiteBucketName"],
            Key=name,
            Body=body,
            ContentType=f"{content_type}; charset=utf-8",
            CacheControl=CACHE.get(suffix, "max-age=300"),
        )
        print(f"Enviado {name}")
    session.client("cloudfront").create_invalidation(
        DistributionId=out["DistributionId"],
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"deploy-{int(time.time())}",
        },
    )
    return out["SiteUrl"]


def publish(session: boto3.Session) -> str:
    response = session.client("lambda").invoke(FunctionName="chuva-bh-publish", Payload=b"{}")
    payload: bytes = response["Payload"].read()
    return payload.decode("utf-8")


def create_user(session: boto3.Session, email: str) -> None:
    out = outputs(session)
    session.client("cognito-idp").admin_create_user(
        UserPoolId=out["UserPoolId"],
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        DesiredDeliveryMediums=["EMAIL"],
    )
    print("Usuário criado; a senha temporária foi enviada por e-mail.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("deploy-site")
    sub.add_parser("publish")
    user = sub.add_parser("create-user")
    user.add_argument("--email", required=True)
    args = parser.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    try:
        if args.command == "deploy-site":
            print(f"Site: {deploy_site(session)}")
        elif args.command == "publish":
            print(publish(session))
        else:
            create_user(session, args.email)
    except (ClientError, BotoCoreError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
