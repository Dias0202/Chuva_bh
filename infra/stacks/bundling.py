"""Empacotamento de código Python com dependências nativas para Linux, sem Docker.

O `uv` baixa wheels já compiladas para a plataforma de destino. Se o `uv` não
estiver no PATH, o CDK cai no bundling com Docker usando a imagem do runtime.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import jsii
from aws_cdk import BundlingOptions, DockerImage, ILocalBundling

# Contexto que desliga o bundling, usado nos testes para não baixar pacotes.
SKIP_BUNDLING_CONTEXT: Final[dict[str, list[str]]] = {"aws:cdk:bundling-stacks": []}
X86_64: Final = "x86_64-manylinux2014"
ARM64: Final = "aarch64-manylinux2014"
_IGNORED: Final = ("__pycache__", "*.pyc")


@jsii.implements(ILocalBundling)
class UvLocalBundling:
    def __init__(
        self,
        source_dir: Path,
        packages: Sequence[str],
        requirements: Sequence[str],
        platform: str,
        python_version: str = "3.12",
    ) -> None:
        self.source_dir = source_dir
        self.packages = tuple(packages)
        self.requirements = tuple(requirements)
        self.platform = platform
        self.python_version = python_version

    # Em tempo de execução o jsii passa o BundlingOptions como segundo argumento posicional,
    # enquanto os stubs de tipo o descrevem como argumentos nomeados. Aceita as duas formas.
    def try_bundle(self, output_dir: str, _options: Any = None, **_kwargs: Any) -> bool:
        uv = shutil.which("uv")
        if uv is None:
            return False
        if self.requirements:
            subprocess.run(  # noqa: S603 - argumentos fixos, sem shell
                [
                    uv,
                    "pip",
                    "install",
                    "--quiet",
                    "--target",
                    output_dir,
                    "--python-platform",
                    self.platform,
                    "--python-version",
                    self.python_version,
                    "--only-binary",
                    ":all:",
                    *self.requirements,
                ],
                check=True,
            )
        for package in self.packages:
            source = self.source_dir / package
            if source.is_file():
                shutil.copy2(source, Path(output_dir) / package)
                continue
            shutil.copytree(
                source,
                Path(output_dir) / package,
                ignore=shutil.ignore_patterns(*_IGNORED),
                dirs_exist_ok=True,
            )
        return True


def bundling_options(
    source_dir: Path,
    packages: Sequence[str],
    requirements: Sequence[str],
    platform: str,
    image: DockerImage,
) -> BundlingOptions:
    copy = " ".join(f"/asset-input/{package}" for package in packages)
    install = (
        f"pip install --no-cache-dir -t /asset-output {' '.join(requirements)} && "
        if requirements
        else ""
    )
    return BundlingOptions(
        image=image,
        command=["bash", "-c", f"{install}cp -r {copy} /asset-output/"],
        local=UvLocalBundling(source_dir, packages, requirements, platform),
    )
