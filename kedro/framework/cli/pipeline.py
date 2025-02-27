"""A collection of CLI commands for working with Kedro pipelines."""
import re
import shutil
from pathlib import Path
from textwrap import indent
from typing import List, NamedTuple, Tuple

import click

import kedro
from kedro.framework.cli.utils import (
    KedroCliError,
    _clean_pycache,
    _filter_deprecation_warnings,
    command_with_verbosity,
    env_option,
)
from kedro.framework.project import settings
from kedro.framework.startup import ProjectMetadata

_SETUP_PY_TEMPLATE = """# -*- coding: utf-8 -*-
from setuptools import setup, find_packages

setup(
    name="{name}",
    version="{version}",
    description="Modular pipeline `{name}`",
    packages=find_packages(),
    include_package_data=True,
    install_requires={install_requires},
)
"""


class PipelineArtifacts(NamedTuple):
    """An ordered collection of source_path, tests_path, config_paths"""

    pipeline_dir: Path
    pipeline_tests: Path
    pipeline_conf: Path


def _assert_pkg_name_ok(pkg_name: str):
    """Check that python package name is in line with PEP8 requirements.

    Args:
        pkg_name: Candidate Python package name.

    Raises:
        KedroCliError: If package name violates the requirements.
    """

    base_message = f"'{pkg_name}' is not a valid Python package name."
    if not re.match(r"^[a-zA-Z_]", pkg_name):
        message = base_message + " It must start with a letter or underscore."
        raise KedroCliError(message)
    if len(pkg_name) < 2:
        message = base_message + " It must be at least 2 characters long."
        raise KedroCliError(message)
    if not re.match(r"^\w+$", pkg_name[1:]):
        message = (
            base_message + " It must contain only letters, digits, and/or underscores."
        )
        raise KedroCliError(message)


def _check_pipeline_name(ctx, param, value):  # pylint: disable=unused-argument
    if value:
        _assert_pkg_name_ok(value)
    return value


# pylint: disable=missing-function-docstring
@click.group(name="Kedro")
def pipeline_cli():  # pragma: no cover
    pass


@pipeline_cli.group()
def pipeline():
    """Commands for working with pipelines."""


@command_with_verbosity(pipeline, "create")
@click.argument("name", nargs=1, callback=_check_pipeline_name)
@click.option(
    "--skip-config",
    is_flag=True,
    help="Skip creation of config files for the new pipeline(s).",
)
@env_option(help="Environment to create pipeline configuration in. Defaults to `base`.")
@click.pass_obj  # this will pass the metadata as first argument
def create_pipeline(
    metadata: ProjectMetadata, name, skip_config, env, **kwargs
):  # pylint: disable=unused-argument
    """Create a new modular pipeline by providing a name."""
    package_dir = metadata.source_dir / metadata.package_name
    conf_source = settings.CONF_SOURCE
    project_conf_path = metadata.project_path / conf_source

    env = env or "base"
    if not skip_config and not (project_conf_path / env).exists():
        raise KedroCliError(
            f"Unable to locate environment '{env}'. "
            f"Make sure it exists in the project configuration."
        )

    result_path = _create_pipeline(name, package_dir / "pipelines")
    _copy_pipeline_tests(name, result_path, package_dir)
    _copy_pipeline_configs(result_path, project_conf_path, skip_config, env=env)
    click.secho(f"\nPipeline '{name}' was successfully created.\n", fg="green")

    click.secho(
        f"To be able to run the pipeline '{name}', you will need to add it "
        f"""to 'register_pipelines()' in '{package_dir / "pipeline_registry.py"}'.""",
        fg="yellow",
    )


@command_with_verbosity(pipeline, "delete")
@click.argument("name", nargs=1, callback=_check_pipeline_name)
@env_option(
    help="Environment to delete pipeline configuration from. Defaults to 'base'."
)
@click.option(
    "-y", "--yes", is_flag=True, help="Confirm deletion of pipeline non-interactively."
)
@click.pass_obj  # this will pass the metadata as first argument
def delete_pipeline(
    metadata: ProjectMetadata, name, env, yes, **kwargs
):  # pylint: disable=unused-argument
    """Delete a modular pipeline by providing a name."""
    package_dir = metadata.source_dir / metadata.package_name
    conf_source = settings.CONF_SOURCE
    project_conf_path = metadata.project_path / conf_source

    env = env or "base"
    if not (project_conf_path / env).exists():
        raise KedroCliError(
            f"Unable to locate environment '{env}'. "
            f"Make sure it exists in the project configuration."
        )

    pipeline_artifacts = _get_pipeline_artifacts(metadata, pipeline_name=name, env=env)

    files_to_delete = [
        pipeline_artifacts.pipeline_conf / confdir / f"{name}.yml"
        for confdir in ("parameters", "catalog")
        if (pipeline_artifacts.pipeline_conf / confdir / f"{name}.yml").is_file()
    ]
    dirs_to_delete = [
        path
        for path in (pipeline_artifacts.pipeline_dir, pipeline_artifacts.pipeline_tests)
        if path.is_dir()
    ]

    if not files_to_delete and not dirs_to_delete:
        raise KedroCliError(f"Pipeline '{name}' not found.")

    if not yes:
        _echo_deletion_warning(
            "The following paths will be removed:",
            directories=dirs_to_delete,
            files=files_to_delete,
        )
        click.echo()
        yes = click.confirm(f"Are you sure you want to delete pipeline '{name}'?")
        click.echo()

    if not yes:
        raise KedroCliError("Deletion aborted!")

    _delete_artifacts(*files_to_delete, *dirs_to_delete)
    click.secho(f"\nPipeline '{name}' was successfully deleted.", fg="green")
    click.secho(
        f"\nIf you added the pipeline '{name}' to 'register_pipelines()' in"
        f""" '{package_dir / "pipeline_registry.py"}', you will need to remove it.""",
        fg="yellow",
    )


def _echo_deletion_warning(message: str, **paths: List[Path]):
    paths = {key: values for key, values in paths.items() if values}

    if paths:
        click.secho(message, bold=True)

    for key, values in paths.items():
        click.echo(f"\n{key.capitalize()}:")
        paths_str = "\n".join(str(value) for value in values)
        click.echo(indent(paths_str, " " * 2))


def _create_pipeline(name: str, output_dir: Path) -> Path:
    with _filter_deprecation_warnings():
        # pylint: disable=import-outside-toplevel
        from cookiecutter.main import cookiecutter

    template_path = Path(kedro.__file__).parent / "templates" / "pipeline"
    cookie_context = {"pipeline_name": name, "kedro_version": kedro.__version__}

    click.echo(f"Creating the pipeline '{name}': ", nl=False)

    try:
        result_path = cookiecutter(
            str(template_path),
            output_dir=str(output_dir),
            no_input=True,
            extra_context=cookie_context,
        )
    except Exception as exc:
        click.secho("FAILED", fg="red")
        cls = exc.__class__
        raise KedroCliError(f"{cls.__module__}.{cls.__qualname__}: {exc}") from exc

    click.secho("OK", fg="green")
    result_path = Path(result_path)
    message = indent(f"Location: '{result_path.resolve()}'", " " * 2)
    click.secho(message, bold=True)

    _clean_pycache(result_path)

    return result_path


# pylint: disable=missing-raises-doc
def _sync_dirs(source: Path, target: Path, prefix: str = "", overwrite: bool = False):
    """Recursively copies `source` directory (or file) into `target` directory without
    overwriting any existing files/directories in the target using the following
    rules:
        1) Skip any files/directories which names match with files in target,
        unless overwrite=True.
        2) Copy all files from source to target.
        3) Recursively copy all directories from source to target.

    Args:
        source: A local directory to copy from, must exist.
        target: A local directory to copy to, will be created if doesn't exist yet.
        prefix: Prefix for CLI message indentation.
    """

    existing = list(target.iterdir()) if target.is_dir() else []
    existing_files = {f.name for f in existing if f.is_file()}
    existing_folders = {f.name for f in existing if f.is_dir()}

    if source.is_dir():
        content = list(source.iterdir())
    elif source.is_file():
        content = [source]
    else:
        # nothing to copy
        content = []  # pragma: no cover

    for source_path in content:
        source_name = source_path.name
        target_path = target / source_name
        click.echo(indent(f"Creating '{target_path}': ", prefix), nl=False)

        if (  # rule #1
            not overwrite
            and source_name in existing_files
            or source_path.is_file()
            and source_name in existing_folders
        ):
            click.secho("SKIPPED (already exists)", fg="yellow")
        elif source_path.is_file():  # rule #2
            try:
                target.mkdir(exist_ok=True, parents=True)
                shutil.copyfile(str(source_path), str(target_path))
            except Exception:
                click.secho("FAILED", fg="red")
                raise
            click.secho("OK", fg="green")
        else:  # source_path is a directory, rule #3
            click.echo()
            new_prefix = (prefix or "") + " " * 2
            _sync_dirs(source_path, target_path, prefix=new_prefix)


def _get_pipeline_artifacts(
    project_metadata: ProjectMetadata, pipeline_name: str, env: str
) -> PipelineArtifacts:
    artifacts = _get_artifacts_to_package(
        project_metadata, f"pipelines.{pipeline_name}", env
    )
    return PipelineArtifacts(*artifacts)


def _get_artifacts_to_package(
    project_metadata: ProjectMetadata, module_path: str, env: str
) -> Tuple[Path, Path, Path]:
    """From existing project, returns in order: source_path, tests_path, config_paths"""
    package_dir = project_metadata.source_dir / project_metadata.package_name
    project_conf_path = project_metadata.project_path / settings.CONF_SOURCE
    artifacts = (
        Path(package_dir, *module_path.split(".")),
        Path(package_dir.parent, "tests", *module_path.split(".")),
        project_conf_path / env,
    )
    return artifacts


def _copy_pipeline_tests(pipeline_name: str, result_path: Path, package_dir: Path):
    tests_source = result_path / "tests"
    tests_target = package_dir.parent / "tests" / "pipelines" / pipeline_name
    try:
        _sync_dirs(tests_source, tests_target)
    finally:
        shutil.rmtree(tests_source)


def _copy_pipeline_configs(
    result_path: Path, conf_path: Path, skip_config: bool, env: str
):
    config_source = result_path / "config"
    try:
        if not skip_config:
            config_target = conf_path / env
            _sync_dirs(config_source, config_target)
    finally:
        shutil.rmtree(config_source)


def _delete_artifacts(*artifacts: Path):
    for artifact in artifacts:
        click.echo(f"Deleting '{artifact}': ", nl=False)
        try:
            if artifact.is_dir():
                shutil.rmtree(artifact)
            else:
                artifact.unlink()
        except Exception as exc:
            click.secho("FAILED", fg="red")
            cls = exc.__class__
            raise KedroCliError(f"{cls.__module__}.{cls.__qualname__}: {exc}") from exc
        else:
            click.secho("OK", fg="green")
