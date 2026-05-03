"""``epublate`` command-line entry point.

Surface:

* bare ``epublate`` — launches the TUI,
* ``epublate version`` — prints the package version,
* ``epublate new SOURCE`` — bootstrap a project folder from an ePub (M1),
* ``epublate open PROJECT`` — open the project on the Reader screen (M2),
* ``epublate export PROJECT --out FILE`` — splice the (possibly partial)
  translation back into a new ePub (M1).

The ``--mock-llm`` global flag pins the provider to the deterministic mock
(PRD NFR-7) by setting ``EPUBLATE_LLM=mock`` for downstream code.

``EPUBLATE_NO_TUI=1`` short-circuits ``open`` to a smoke-print so headless
CLI tests can assert wiring without booting Textual.

``.env`` loading is wired through :mod:`epublate.app.dotenv`: the cwd
file is loaded once in the group callback, and project-dir files are
loaded by ``_PROJECT_DIR_TYPE`` as soon as Click resolves the argument.
``override=False`` means real shell variables always win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

from epublate import __version__
from epublate.app.dotenv import load_dotenv_files


class _ProjectDirParam(click.Path):
    """``click.Path`` for project-dir args that side-loads ``.env``.

    After Click resolves the path we run :func:`load_dotenv_files` with
    that directory so any ``<project_dir>/.env`` is applied to the
    process environment *before* the subcommand body executes (i.e.
    before :mod:`epublate.llm.factory` reads ``EPUBLATE_LLM_*``).
    """

    def convert(
        self,
        value: Any,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> Any:
        resolved = super().convert(value, param, ctx)
        if isinstance(resolved, Path):
            load_dotenv_files(project_dir=resolved)
        return resolved


_PROJECT_DIR_TYPE = _ProjectDirParam(
    exists=True, file_okay=False, path_type=Path, resolve_path=True
)


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--mock-llm",
    "mock_llm",
    is_flag=True,
    default=False,
    help="Force the deterministic mock LLM provider (no network).",
)
@click.version_option(__version__, "-V", "--version", prog_name="epublate")
@click.pass_context
def main(ctx: click.Context, mock_llm: bool) -> None:
    """Translate ePub story books with an LLM, preserving format and lore."""

    # Load ``./.env`` before any subcommand reads ``EPUBLATE_LLM_*``.
    # ``--mock-llm`` is applied *after* so the flag still beats a
    # ``.env`` that pins a real endpoint.
    load_dotenv_files()

    ctx.ensure_object(dict)
    ctx.obj["mock_llm"] = mock_llm
    if mock_llm:
        os.environ["EPUBLATE_LLM"] = "mock"

    if ctx.invoked_subcommand is None:
        from epublate.app.main import run
        from epublate.app.screens.reader import DEFAULT_MODEL
        from epublate.llm.factory import ENV_MODEL

        # Mirror ``epublate open <project>``: env wins over the hardcoded
        # fallback so the bare TUI honors ``EPUBLATE_LLM_MODEL`` /
        # ``.env`` instead of always defaulting to ``DEFAULT_MODEL``.
        run(default_model=os.environ.get(ENV_MODEL) or DEFAULT_MODEL)


@main.command()
def version() -> None:
    """Print the installed epublate version."""

    click.echo(__version__)


@main.command()
@click.argument(
    "source",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path, resolve_path=True),
    default=None,
    help="Project folder to create. Defaults to ./<book-stem>/.",
)
@click.option(
    "--source-lang",
    "source_lang",
    default="und",
    show_default=True,
    help="Source language code (BCP-47).",
)
@click.option(
    "--target-lang",
    "target_lang",
    required=True,
    help="Target language code (BCP-47).",
)
@click.option(
    "--name",
    "name",
    default=None,
    help="Project display name. Defaults to the source ePub's stem.",
)
@click.option(
    "--starter-glossary",
    "starter_glossary",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
    default=None,
    help="Optional JSON glossary to seed the lore bible (PRD \u00a77.1 / M3).",
)
@click.option(
    "--intake/--no-intake",
    "intake_enabled",
    default=False,
    show_default=True,
    help=(
        "Run the helper-LLM book intake pass after creating the project "
        "(PRD \u00a77.1 / M5). Off by default because it costs LLM tokens."
    ),
)
@click.option(
    "--helper-model",
    "helper_model",
    default=None,
    help=(
        "Helper model for intake. Defaults to $EPUBLATE_LLM_HELPER_MODEL "
        "or the translator model (PRD F-LLM-2)."
    ),
)
@click.option(
    "--intake-max-segments",
    "intake_max_segments",
    type=click.IntRange(min=1),
    default=None,
    help="Cap on segments fed to the intake helper (default: first 30).",
)
@click.option(
    "--style-profile",
    "style_profile",
    default=None,
    help=(
        "Tone preset to seed the translator's system prompt with "
        "(PRD F-STYLE-1). Defaults to 'literary_fiction'. Use "
        "'list' to print the shipped presets and exit. Pass "
        "'none' to opt out and leave the style guide empty."
    ),
)
@click.option(
    "--style-text",
    "style_text",
    default=None,
    help=(
        "Free-form style guide text. Overrides the resolved preset; "
        "use this when you want to author the prompt yourself."
    ),
)
@click.pass_context
def new(
    ctx: click.Context,
    source: Path,
    out_dir: Path | None,
    source_lang: str,
    target_lang: str,
    name: str | None,
    starter_glossary: Path | None,
    intake_enabled: bool,
    helper_model: str | None,
    intake_max_segments: int | None,
    style_profile: str | None,
    style_text: str | None,
) -> None:
    """Create a new project from an ePub source (PRD §7.1 / M1)."""

    from epublate.app.paths import (
        default_projects_root,
        ensure_projects_root,
        unique_project_dir,
    )
    from epublate.core.extractor import (
        DEFAULT_INTAKE_MAX_SEGMENTS,
        IntakeOptions,
    )
    from epublate.core.project import Project
    from epublate.core.style import (
        DEFAULT_STYLE_PROFILE,
        PROFILE_REGISTRY,
        list_profiles,
    )

    if style_profile is not None and style_profile.lower() == "list":
        click.echo("Available tone presets (default: literary_fiction):")
        for prof in list_profiles():
            marker = " *" if prof.id == DEFAULT_STYLE_PROFILE else "  "
            click.echo(f"{marker} {prof.id:<22} {prof.description}")
        ctx.exit(0)
    resolved_profile: str | None
    if style_profile is None:
        resolved_profile = DEFAULT_STYLE_PROFILE
    elif style_profile.lower() in {"none", "off", ""}:
        resolved_profile = None
    elif style_profile not in PROFILE_REGISTRY:
        raise click.BadParameter(
            f"unknown style profile {style_profile!r}; pass --style-profile list "
            "to see the shipped presets."
        )
    else:
        resolved_profile = style_profile

    if out_dir is not None:
        target_dir = out_dir
    else:
        # Land new projects in the user's projects root (see
        # ``app/paths.py``), not ``$CWD`` — keeps the repo clean when
        # ``epublate new`` is run from a source checkout and gives
        # curators a single predictable "where did my book go?" answer.
        root = ensure_projects_root(default_projects_root())
        target_dir = unique_project_dir(root, source.stem or "project")
    intake_options: IntakeOptions | None = None
    intake_provider: object | None = None
    if intake_enabled:
        from epublate.llm.factory import (
            ENV_MODEL,
            build_provider,
            resolve_helper_model,
        )

        translator_model = os.environ.get(ENV_MODEL)
        chosen_helper = resolve_helper_model(translator_model, override=helper_model)
        intake_options = IntakeOptions(
            model=chosen_helper,
            max_segments=intake_max_segments or DEFAULT_INTAKE_MAX_SEGMENTS,
        )
        intake_provider = build_provider(mock=bool(ctx.obj.get("mock_llm")))

    project = Project.create(
        source,
        out_dir=target_dir,
        source_lang=source_lang,
        target_lang=target_lang,
        name=name,
        starter_glossary=starter_glossary,
        intake=intake_options,
        intake_provider=intake_provider,  # type: ignore[arg-type]
        style_profile=resolved_profile,
        style_guide=style_text,
    )
    _record_recent(project)
    try:
        click.echo(f"Created project {project.name!r} at {project.project_dir}")
        click.echo(f"  database: {project.db_path}")
        click.echo(f"  original: {project.original_epub_path}")
        from epublate.core.style import label_for as _style_label

        active_profile = project.style_profile
        click.echo(
            f"  tone     : {_style_label(active_profile)}"
            + (
                ""
                if project.style_guide is None
                else f" — {len(project.style_guide)} chars in style guide"
            )
        )
        if starter_glossary is not None:
            click.echo(f"  starter glossary: {starter_glossary}")
        if project.last_intake_summary is not None:
            s = project.last_intake_summary
            click.echo(
                f"  intake: {s.chunks} chunks ({s.cached_chunks} cached), "
                f"{s.proposed_count} proposed entries, "
                f"${s.cost_usd:.4f} spent"
                + (f" — pov={s.pov!r}" if s.pov else "")
                + (f", tense={s.tense!r}" if s.tense else "")
            )
            if s.register or s.audience:
                click.echo(
                    "  helper observations: "
                    + ", ".join(
                        part
                        for part in (
                            f"register={s.register!r}" if s.register else "",
                            f"audience={s.audience!r}" if s.audience else "",
                        )
                        if part
                    )
                )
            if (
                s.suggested_style_profile is not None
                and s.suggested_style_profile != active_profile
            ):
                tone_label = _style_label(s.suggested_style_profile)
                click.echo(
                    f"  helper suggests tone: {tone_label} "
                    f"({s.suggested_style_profile})"
                )
                click.echo(
                    "    apply with: epublate intake "
                    + click.format_filename(str(project.project_dir))
                    + "  (then Settings → Edit style)"
                )
    finally:
        project.close()


@main.command(name="open")
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option(
    "--model",
    "model",
    default=None,
    help=(
        "Translator model. Defaults to $EPUBLATE_LLM_MODEL or "
        "the Reader's built-in default."
    ),
)
@click.pass_context
def open_cmd(ctx: click.Context, project_dir: Path, model: str | None) -> None:
    """Open the Project Dashboard on an existing project (PRD §4.6 / M4).

    The Dashboard is the new landing screen; press ``o`` to enter the
    Reader, ``g`` for Glossary, ``i`` for Inbox.
    """

    from epublate.app.config import UIConfig
    from epublate.app.main import run as run_app
    from epublate.app.screens.dashboard import DashboardScreen
    from epublate.app.screens.reader import DEFAULT_MODEL
    from epublate.core.project import Project
    from epublate.db import repo
    from epublate.llm.factory import ENV_MODEL, build_provider

    project = Project.open(project_dir)
    _record_recent(project)
    overrides = repo.get_llm_overrides(project.engine, project.project_id)
    chosen_model = (
        model
        or str(overrides.get("translator_model") or "").strip()
        or os.environ.get(ENV_MODEL)
        or DEFAULT_MODEL
    )
    use_mock = bool(ctx.obj.get("mock_llm"))
    ui_config = UIConfig.load()

    if os.environ.get("EPUBLATE_NO_TUI") == "1":
        try:
            click.echo(
                f"Opening Dashboard on {project.project_dir} (model={chosen_model})"
            )
        finally:
            project.close()
        return

    def _provider_factory() -> object:
        # Reread overrides on every build so a Settings edit during
        # the session affects the next worker without a restart.
        live_overrides = repo.get_llm_overrides(project.engine, project.project_id)
        return build_provider(mock=use_mock, overrides=live_overrides)

    screen = DashboardScreen(
        project,
        provider_factory=_provider_factory,  # type: ignore[arg-type]
        default_model=chosen_model,
        ui_config=ui_config,
    )
    try:
        run_app(initial_screen=screen)
    finally:
        project.close()


@main.command()
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path, resolve_path=True),
    required=True,
    help="Path to write the exported ePub.",
)
@click.option(
    "--strict/--no-strict",
    "strict",
    default=False,
    show_default=True,
    help=(
        "Run epubcheck on the exported file and exit non-zero if it "
        "reports any errors (PRD F-IO-6 / M6). Implies --epubcheck."
    ),
)
@click.option(
    "--epubcheck/--no-epubcheck",
    "epubcheck",
    default=False,
    show_default=True,
    help=(
        "Run epubcheck post-export and surface its summary. Requires "
        "the optional ``[epubcheck]`` extra (`uv sync --extra epubcheck`)."
    ),
)
@click.pass_context
def export(
    ctx: click.Context,
    project_dir: Path,
    out_path: Path,
    strict: bool,
    epubcheck: bool,
) -> None:
    """Export the (possibly partial) translated ePub (PRD §7.6 / M1, F-IO-6)."""

    from epublate.core.project import open_project

    run_validation = strict or epubcheck

    with open_project(project_dir) as project:
        written = project.export(out_path, epubcheck=run_validation)
        report = project.last_epubcheck_report

    click.echo(f"Wrote {written}")
    if run_validation and report is not None:
        click.echo(report.summary_line())
        if report.status == "skipped" and strict:
            click.echo(
                "  hint: --strict has no teeth without the [epubcheck] extra "
                "and a working JRE.",
                err=True,
            )
        if strict and report.has_errors:
            for msg in report.messages[:5]:
                click.echo(f"  {msg.short()}", err=True)
            ctx.exit(3)


@main.command()
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option(
    "--chapters",
    "chapters",
    default="*",
    show_default=True,
    help="Chapter range to translate. Use '*' for all, 'N' for one, 'A-B' for a "
    "1-indexed range over translatable chapters.",
)
@click.option(
    "--concurrency",
    "concurrency",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Number of concurrent worker threads (PRD F-LLM-5).",
)
@click.option(
    "--budget",
    "budget",
    type=click.FloatRange(min=0),
    default=None,
    help="Optional per-batch USD budget cap. Overrides the project budget.",
)
@click.option(
    "--model",
    "model",
    default=None,
    help=("Translator model. Defaults to $EPUBLATE_LLM_MODEL or the built-in default."),
)
@click.option(
    "--bypass-cache",
    "bypass_cache",
    is_flag=True,
    default=False,
    help="Force re-translation, ignoring the cache.",
)
@click.option(
    "--extract/--no-extract",
    "pre_pass",
    default=False,
    show_default=True,
    help=(
        "Run the helper-LLM pre-pass before each chapter (PRD \u00a74.2 / M5). "
        "Surfaces fresh proper-noun candidates as proposed glossary entries."
    ),
)
@click.option(
    "--helper-model",
    "helper_model",
    default=None,
    help=(
        "Helper model for the pre-pass. Defaults to $EPUBLATE_LLM_HELPER_MODEL "
        "or the translator model."
    ),
)
@click.option(
    "--group-small/--no-group-small",
    "group_small",
    default=True,
    show_default=True,
    help=(
        "Batch short, lightly-marked-up segments (e.g. table of contents, "
        "index entries — including those wrapped in <a> links) into a "
        "single LLM call to cut round-trips. Disable if your provider "
        "doesn't play nicely with list-style JSON."
    ),
)
@click.option(
    "--group-max-items",
    "group_max_items",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Cap on the number of segments packed into one grouped call. "
        "Defaults to 50. Larger groups save more round-trips but make a "
        "parse failure more expensive to re-fan-out."
    ),
)
@click.pass_context
def batch(
    ctx: click.Context,
    project_dir: Path,
    chapters: str,
    concurrency: int,
    budget: float | None,
    model: str | None,
    bypass_cache: bool,
    pre_pass: bool,
    helper_model: str | None,
    group_small: bool,
    group_max_items: int | None,
) -> None:
    """Headless batch translation (PRD §7.3 / M4).

    Selects pending segments in ``--chapters`` and runs the pipeline at
    the configured ``--concurrency``. Failures don't abort the batch;
    they're recorded on the summary and emitted to the event log so the
    Inbox can surface them. Exits with code 2 when the run pauses on
    the budget cap.
    """

    from epublate.app.screens.reader import DEFAULT_MODEL
    from epublate.core.batch import BatchOptions, BatchPaused, run_batch
    from epublate.core.pipeline import GROUP_DEFAULT_MAX_ITEMS
    from epublate.core.project import open_project
    from epublate.db import repo as repo_mod
    from epublate.llm.factory import ENV_MODEL, build_provider

    chosen_model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
    use_mock = bool(ctx.obj.get("mock_llm"))

    chosen_helper: str | None = None
    if pre_pass:
        from epublate.llm.factory import resolve_helper_model

        chosen_helper = resolve_helper_model(chosen_model, override=helper_model)

    with open_project(project_dir) as project:
        chapter_ids = _resolve_chapter_ids(project, chapters)
        if chapter_ids is None and chapters not in ("*", ""):
            raise click.UsageError(
                f"Could not parse --chapters {chapters!r}. Use '*', 'N' or 'A-B'."
            )
        provider = build_provider(mock=use_mock)
        options = BatchOptions(
            model=chosen_model,
            concurrency=concurrency,
            budget_usd=budget,
            chapter_ids=chapter_ids,
            bypass_cache=bypass_cache,
            pre_pass=pre_pass,
            helper_model=chosen_helper,
            group_small_segments=group_small,
            group_max_items=group_max_items or GROUP_DEFAULT_MAX_ITEMS,
        )
        paused = False
        try:
            summary = run_batch(
                engine=project.engine,
                project_id=project.project_id,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
                provider=provider,
                options=options,
                on_progress=lambda ev: _print_batch_tick(ev),
            )
        except BatchPaused as exc:
            summary = exc.summary
            paused = True

        click.echo(_format_batch_summary(summary, paused=paused))
        # Emit a stats summary so the curator sees the project state.
        del repo_mod  # reserved for future stats wiring
    if paused:
        ctx.exit(2)


def _print_batch_tick(event: object) -> None:
    """Render one progress tick on stderr without blocking the run."""

    # Imported lazily to keep the CLI module slim; the worker thread
    # invokes this so we keep formatting trivial.
    from epublate.core.batch import BatchProgressEvent

    if not isinstance(event, BatchProgressEvent):
        return
    summary = event.summary
    label = "ok" if event.error is None else "FAIL"
    suffix = f" — {event.error}" if event.error else ""
    click.echo(
        f"  [{label}] segment {event.segment_id[:8]} "
        f"(translated={summary.translated} cached={summary.cached} "
        f"flagged={summary.flagged} failed={summary.failed} "
        f"cost=${summary.cost_usd:.4f}){suffix}",
        err=True,
    )


def _format_batch_summary(summary: object, *, paused: bool) -> str:
    from epublate.core.batch import BatchSummary

    if not isinstance(summary, BatchSummary):
        return "(no summary)"
    header = "Batch paused (budget cap)" if paused else "Batch complete"
    lines = [
        header,
        f"  attempted    : {summary.attempted}",
        f"  translated   : {summary.translated}",
        f"  cached       : {summary.cached}",
        f"  flagged      : {summary.flagged}",
        f"  failed       : {summary.failed}",
        f"  prompt_tokens: {summary.prompt_tokens}",
        f"  completion_tokens: {summary.completion_tokens}",
        f"  cost_usd     : ${summary.cost_usd:.4f}",
        f"  elapsed_s    : {summary.elapsed_s:.2f}",
    ]
    if summary.pre_pass is not None:
        pp = summary.pre_pass
        lines.append(
            f"  pre_pass     : {pp.chunks} chunks "
            f"({pp.cached_chunks} cached), {pp.proposed_count} proposed, "
            f"${pp.cost_usd:.4f}"
        )
    if paused and summary.paused_reason:
        lines.append(f"  paused_reason: {summary.paused_reason}")
    return "\n".join(lines)


def _resolve_chapter_ids(project: object, expr: str) -> tuple[str, ...] | None:
    """Mirror :meth:`DashboardScreen._resolve_chapter_range` for the CLI."""

    from epublate.core.project import Project
    from epublate.db import repo as repo_mod

    if not isinstance(project, Project):
        return None
    chapters = repo_mod.list_chapters(project.engine, project.project_id)
    if not chapters:
        return ()
    expr = expr.strip()
    if expr in ("", "*"):
        return None
    try:
        if "-" in expr:
            lo_s, hi_s = expr.split("-", 1)
            lo = int(lo_s)
            hi = int(hi_s)
        else:
            lo = hi = int(expr)
    except ValueError:
        return None
    if lo < 1 or hi < lo or hi > len(chapters):
        return None
    return tuple(chapters[i - 1].id for i in range(lo, hi + 1))


@main.command()
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option(
    "--kind",
    "kind",
    type=click.Choice(["all", "flagged", "proposed", "alerts"]),
    default="all",
    show_default=True,
    help="Restrict the listing to one Inbox section.",
)
def inbox(project_dir: Path, kind: str) -> None:
    """List flagged segments, proposed entries, and alerts (PRD §4.6 / M4)."""

    from epublate.core.project import open_project
    from epublate.core.stats import flagged_segments, recent_alerts
    from epublate.db import repo as repo_mod

    with open_project(project_dir) as project:
        if kind in ("all", "flagged"):
            flagged = flagged_segments(project.engine, project_id=project.project_id)
            click.echo(f"Flagged segments ({len(flagged)}):")
            for seg in flagged:
                preview = seg.source_text.replace("\n", " ").strip()
                if len(preview) > 60:
                    preview = preview[:59] + "…"
                click.echo(f"  - {seg.id[:8]}  idx={seg.idx}  {preview}")
        if kind in ("all", "proposed"):
            proposed = repo_mod.list_glossary_entries(
                project.engine, project.project_id, status="proposed"
            )
            click.echo(f"Proposed glossary entries ({len(proposed)}):")
            for entry in proposed:
                click.echo(f"  - {entry.entry.type:<12} {entry.source_term}")
        if kind in ("all", "alerts"):
            alerts = recent_alerts(
                project.engine, project_id=project.project_id, limit=20
            )
            click.echo(f"Recent alerts ({len(alerts)}):")
            for ev in alerts:
                click.echo(f"  - {ev.kind}  {ev.payload}")


@main.command()
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option(
    "--helper-model",
    "helper_model",
    default=None,
    help=(
        "Helper model. Defaults to $EPUBLATE_LLM_HELPER_MODEL or the translator model."
    ),
)
@click.option(
    "--max-segments",
    "max_segments",
    type=click.IntRange(min=1),
    default=None,
    help="Cap on segments fed to the helper (default: first 30).",
)
@click.option(
    "--bypass-cache",
    "bypass_cache",
    is_flag=True,
    default=False,
    help="Force re-extraction, ignoring cached helper calls.",
)
@click.pass_context
def intake(
    ctx: click.Context,
    project_dir: Path,
    helper_model: str | None,
    max_segments: int | None,
    bypass_cache: bool,
) -> None:
    """Run the helper-LLM book-intake pass on an existing project (PRD §7.1 / M5).

    Equivalent to ``epublate new --intake`` but for projects that were
    created without it. Re-runs are cheap on a populated cache —
    second-time hits don't call the network (PRD F-LLM-6).
    """

    from epublate.core.extractor import (
        DEFAULT_INTAKE_MAX_SEGMENTS,
        IntakeOptions,
        run_book_intake,
    )
    from epublate.core.project import open_project
    from epublate.llm.factory import ENV_MODEL, build_provider, resolve_helper_model

    use_mock = bool(ctx.obj.get("mock_llm"))
    translator_model = os.environ.get(ENV_MODEL)
    chosen_helper = resolve_helper_model(translator_model, override=helper_model)
    options = IntakeOptions(
        model=chosen_helper,
        max_segments=max_segments or DEFAULT_INTAKE_MAX_SEGMENTS,
        bypass_cache=bypass_cache,
    )
    provider = build_provider(mock=use_mock)

    with open_project(project_dir) as project:
        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=options,
        )

    click.echo("Intake complete")
    click.echo(f"  chunks       : {summary.chunks} ({summary.cached_chunks} cached)")
    click.echo(f"  proposed     : {summary.proposed_count}")
    click.echo(f"  failed_chunks: {summary.failed_chunks}")
    click.echo(
        f"  tokens       : "
        f"{summary.prompt_tokens} prompt + {summary.completion_tokens} completion"
    )
    click.echo(f"  cost_usd     : ${summary.cost_usd:.4f}")
    if summary.pov:
        click.echo(f"  pov          : {summary.pov}")
    if summary.tense:
        click.echo(f"  tense        : {summary.tense}")
    if summary.register:
        click.echo(f"  register     : {summary.register}")
    if summary.audience:
        click.echo(f"  audience     : {summary.audience}")
    if summary.suggested_style_profile is not None:
        from epublate.core.style import label_for as _style_label

        click.echo(
            f"  suggested tone: {_style_label(summary.suggested_style_profile)} "
            f"({summary.suggested_style_profile})"
        )
        click.echo("    apply via Settings → Edit style.")


@main.command()
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def stats(project_dir: Path, as_json: bool) -> None:
    """Print project counters and spend (PRD F-T-2 / M4)."""

    import json as _json

    from epublate.core.project import open_project
    from epublate.core.stats import compute_stats

    with open_project(project_dir) as project:
        s = compute_stats(project.engine, project.project_id)

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "project_id": s.project_id,
                    "budget_usd": s.budget_usd,
                    "spend_usd": s.spend_usd,
                    "prompt_tokens": s.prompt_tokens,
                    "completion_tokens": s.completion_tokens,
                    "llm_calls": s.llm_calls,
                    "cache_hits": s.cache_hits,
                    "cache_hit_rate": s.cache_hit_rate,
                    "chapter_count": s.chapter_count,
                    "segment_count": s.segment_count,
                    "segments_by_status": s.segments_by_status,
                    "spend_by_model": s.spend_by_model,
                    "validation_failure_rate": s.validation_failure_rate,
                    "progress_ratio": s.progress_ratio,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    click.echo(f"project_id      : {s.project_id}")
    click.echo(
        f"budget_usd      : "
        f"{f'${s.budget_usd:.4f}' if s.budget_usd is not None else '(none)'}"
    )
    click.echo(f"spend_usd       : ${s.spend_usd:.4f}")
    click.echo(
        f"tokens          : {s.prompt_tokens} prompt + {s.completion_tokens} completion"
    )
    click.echo(f"llm_calls       : {s.llm_calls}  (cache hits {s.cache_hits})")
    click.echo(f"cache_hit_rate  : {s.cache_hit_rate * 100:.1f}%")
    click.echo(f"chapters        : {s.chapter_count}")
    click.echo(f"segments        : {s.segment_count}")
    click.echo(f"progress        : {s.progress_ratio * 100:.1f}%")
    click.echo(f"validation_fail : {s.validation_failure_rate * 100:.1f}%")
    click.echo("segments_by_status:")
    for status_key, count in sorted(s.segments_by_status.items()):
        click.echo(f"  {status_key:<12} {count}")
    if s.spend_by_model:
        click.echo("spend_by_model:")
        for model_key, cost in sorted(s.spend_by_model.items()):
            click.echo(f"  {model_key:<24} ${cost:.4f}")


@main.group(name="budget")
def budget_cmd() -> None:
    """Read or update the per-project USD budget cap (PRD F-LLM-8 / M4)."""


@budget_cmd.command(name="show")
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
def budget_show(project_dir: Path) -> None:
    from epublate.core.project import open_project
    from epublate.core.stats import compute_spend

    with open_project(project_dir) as project:
        from epublate.db import repo as repo_mod

        row = repo_mod.get_project(project.engine, project.project_id)
        spend = compute_spend(project.engine, project.project_id)

    if row is None:
        raise click.ClickException("project row not found")
    if row.budget_usd is None:
        click.echo(f"budget : (none)\nspent  : ${spend:.4f}")
    else:
        remaining = max(0.0, row.budget_usd - spend)
        click.echo(
            f"budget   : ${row.budget_usd:.4f}\n"
            f"spent    : ${spend:.4f}\n"
            f"remaining: ${remaining:.4f}"
        )


@budget_cmd.command(name="set")
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.argument("amount", type=click.FloatRange(min=0))
def budget_set(project_dir: Path, amount: float) -> None:
    from epublate.core.project import open_project
    from epublate.db import repo as repo_mod

    with open_project(project_dir) as project:
        repo_mod.update_project_budget(
            project.engine,
            project_id=project.project_id,
            budget_usd=amount,
        )
    click.echo(f"Budget set to ${amount:.4f}.")


@budget_cmd.command(name="clear")
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
def budget_clear(project_dir: Path) -> None:
    from epublate.core.project import open_project
    from epublate.db import repo as repo_mod

    with open_project(project_dir) as project:
        repo_mod.update_project_budget(
            project.engine,
            project_id=project.project_id,
            budget_usd=None,
        )
    click.echo("Budget cleared.")


@main.group(name="glossary")
def glossary_cmd() -> None:
    """Glossary import / export (PRD F-LB-8 / M3)."""


@glossary_cmd.command(name="export")
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path, resolve_path=True),
    required=True,
    help="Destination file (will be overwritten).",
)
def glossary_export(project_dir: Path, out_path: Path) -> None:
    """Write the project's glossary to ``OUT_PATH`` as JSON."""

    from epublate.core.project import open_project
    from epublate.glossary import io as glossary_io

    with open_project(project_dir) as project:
        payload = glossary_io.export_json(project.engine, project.project_id)
    glossary_io.write_export(out_path, payload)
    click.echo(f"Exported {len(payload['entries'])} entries to {out_path}")


@glossary_cmd.command(name="import")
@click.argument(
    "project_dir",
    type=_PROJECT_DIR_TYPE,
)
@click.argument(
    "in_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
)
@click.option(
    "--conflict",
    "conflict",
    type=click.Choice(["skip", "overwrite"]),
    default="skip",
    show_default=True,
    help="Behavior when an entry with the same source_term already exists.",
)
def glossary_import(project_dir: Path, in_path: Path, conflict: str) -> None:
    """Import a glossary JSON file into ``PROJECT_DIR``."""

    from epublate.core.project import open_project
    from epublate.glossary import io as glossary_io

    payload = glossary_io.read_payload(in_path)
    with open_project(project_dir) as project:
        summary = glossary_io.import_json(
            project.engine,
            project_id=project.project_id,
            payload=payload,
            conflict=conflict,  # type: ignore[arg-type]
        )
    click.echo(
        f"Imported {summary.created} created, "
        f"{summary.updated} updated, "
        f"{summary.skipped} skipped"
    )


def _record_recent(project: object) -> None:
    """Best-effort: keep the TUI's recents store in sync with CLI activity.

    Failure here is silent; the CLI must still succeed for users without
    a writable XDG config dir (e.g. CI sandboxes).
    """

    from epublate.core.project import Project as _Project

    if not isinstance(project, _Project):
        return
    try:
        from epublate.app.recents import record_project

        record_project(
            project_dir=project.project_dir,
            name=project.name,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
        )
    except Exception:
        # Recents are a UX nicety, never a hard CLI dependency.
        pass


# ---------------------------------------------------------------------------
# Lore Book commands (PRD §4.3 / F-LB-10)
# ---------------------------------------------------------------------------


_LORE_DIR_TYPE = click.Path(
    exists=True, file_okay=False, path_type=Path, resolve_path=True
)


@main.group(name="lore")
def lore_cmd() -> None:
    """Manage portable Lore Books (PRD §4.3 / F-LB-10).

    Lore Books are stand-alone lore bibles kept on disk so they can be
    attached to multiple translation projects (e.g. every book in a
    series). Each Lore Book lives in its own ``<name>.epublate-lore/``
    folder with a single SQLite DB.
    """


@lore_cmd.command(name="new")
@click.option("--name", "name", required=True, help="Display name for the Lore Book.")
@click.option(
    "--source-lang",
    "source_lang",
    required=True,
    help="Source language code (BCP-47).",
)
@click.option(
    "--target-lang",
    "target_lang",
    required=True,
    help="Target language code (BCP-47).",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path, resolve_path=True),
    default=None,
    help=(
        "Folder to create. Defaults to <library>/<slug>.epublate-lore "
        "where library is $EPUBLATE_LORE_LIBRARY or "
        "~/.config/epublate/lore."
    ),
)
@click.option(
    "--description",
    "description",
    default=None,
    help="Short note describing the Lore Book's scope.",
)
@click.option(
    "--from-project",
    "from_project",
    type=click.Path(exists=True, file_okay=False, path_type=Path, resolve_path=True),
    default=None,
    help=(
        "Bootstrap the new Lore Book from this project's curated glossary "
        "(e.g. an already-translated book in the same series)."
    ),
)
def lore_new(
    name: str,
    source_lang: str,
    target_lang: str,
    out_dir: Path | None,
    description: str | None,
    from_project: Path | None,
) -> None:
    """Create an empty Lore Book.

    With ``--from-project``, the freshly created Lore Book is
    pre-populated from the given project's glossary so the curator can
    start a series-wide lore corpus from book one's lore bible without
    a separate import step. A fresh Lore Book has no entries to clash
    with, so the import implicitly uses an ``overwrite``-equivalent
    policy (every row in the project becomes a row in the new book).
    """

    from epublate.lore import (
        LoreBook,
        default_library_dir,
        import_project_glossary,
    )

    if out_dir is None:
        slug = _slugify_lore_name(name) or "lore"
        out_dir = default_library_dir() / f"{slug}.epublate-lore"

    book = LoreBook.create(
        out_dir=out_dir,
        name=name,
        source_lang=source_lang,
        target_lang=target_lang,
        description=description,
    )
    try:
        click.echo(f"Created Lore Book {book.name!r} at {book.lore_dir}")
        click.echo(f"  database  : {book.db_path}")
        click.echo(f"  languages : {book.source_lang} -> {book.target_lang}")
        if from_project is not None:
            summary = import_project_glossary(
                book, src_project_dir=from_project, policy="overwrite"
            )
            click.echo(
                f"  bootstrap : imported {summary.created} entries "
                f"from {from_project}"
                + (
                    f" (target-only: {summary.target_only_inserts})"
                    if summary.target_only_inserts
                    else ""
                )
            )
    finally:
        book.close()


@lore_cmd.command(name="open")
@click.argument("lore_dir", type=_LORE_DIR_TYPE)
def lore_open(lore_dir: Path) -> None:
    """Print summary info about an existing Lore Book.

    The TUI is the canonical edit surface for Lore Books; this command
    prints a one-shot summary so headless scripts can sanity-check
    that a Lore Book directory is readable.
    """

    from epublate.db import repo as _repo
    from epublate.lore import LoreBook, list_lore_sources

    book = LoreBook.open(lore_dir)
    try:
        entries = _repo.list_glossary_entries(book.engine, book.project_id)
        sources = list_lore_sources(book.engine, project_id=book.project_id)
        statuses: dict[str, int] = {}
        for ent in entries:
            statuses[ent.status] = statuses.get(ent.status, 0) + 1
        target_only = sum(1 for ent in entries if not ent.source_known)
        click.echo(f"Lore Book   : {book.name}")
        click.echo(f"  path       : {book.lore_dir}")
        click.echo(f"  languages  : {book.source_lang} -> {book.target_lang}")
        if book.description:
            click.echo(f"  description: {book.description}")
        click.echo(f"  entries    : {len(entries)} (target-only: {target_only})")
        for st in ("locked", "confirmed", "proposed"):
            click.echo(f"    {st:<10}: {statuses.get(st, 0)}")
        click.echo(f"  ingested   : {len(sources)} ePub(s)")
        for src in sources[-5:]:
            click.echo(
                f"    - {src.kind:<6} {src.epub_path}  "
                f"(+{src.entries_added} entries, status={src.status})"
            )
    finally:
        book.close()


@lore_cmd.command(name="list")
def lore_list() -> None:
    """List Lore Books in the configured library."""

    from epublate.lore import default_library_dir, iter_library_lore_books

    library = default_library_dir()
    handles = list(iter_library_lore_books(library))
    click.echo(f"Library: {library}")
    if not handles:
        click.echo("  (no Lore Books yet — try `epublate lore new`)")
        return
    for handle in handles:
        click.echo(f"  - {handle.name}  [{handle.lore_dir}]")


@lore_cmd.group(name="ingest")
def lore_ingest_cmd() -> None:
    """Ingest an ePub into a Lore Book to seed proper-noun proposals."""


@lore_ingest_cmd.command(name="source")
@click.argument("lore_dir", type=_LORE_DIR_TYPE)
@click.argument(
    "epub_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
)
@click.option(
    "--helper-model",
    "helper_model",
    default=None,
    help=(
        "Helper model. Defaults to $EPUBLATE_LLM_HELPER_MODEL or the translator model."
    ),
)
@click.option(
    "--max-segments",
    "max_segments",
    type=click.IntRange(min=1),
    default=None,
    help="Cap on segments fed to the helper (default: first 30).",
)
@click.pass_context
def lore_ingest_source(
    ctx: click.Context,
    lore_dir: Path,
    epub_path: Path,
    helper_model: str | None,
    max_segments: int | None,
) -> None:
    """Ingest a *source-language* ePub into ``LORE_DIR``.

    Reuses the same proper-noun extractor the project intake pass uses,
    so source-side and target-side knowledge accumulate in one place.
    """

    from epublate.core.extractor import DEFAULT_INTAKE_MAX_SEGMENTS
    from epublate.llm.factory import ENV_MODEL, build_provider, resolve_helper_model
    from epublate.lore import LoreBook, ingest_source_epub

    use_mock = bool(ctx.obj.get("mock_llm"))
    translator_model = os.environ.get(ENV_MODEL)
    chosen_helper = resolve_helper_model(translator_model, override=helper_model)
    provider = build_provider(mock=use_mock)

    book = LoreBook.open(lore_dir)
    try:
        summary = ingest_source_epub(
            book,
            epub_path=epub_path,
            provider=provider,
            helper_model=chosen_helper,
            max_segments=max_segments or DEFAULT_INTAKE_MAX_SEGMENTS,
        )
    finally:
        book.close()

    click.echo("Source ingest complete")
    click.echo(f"  proposed     : {summary.proposed_count}")
    if summary.intake_summary is not None:
        s = summary.intake_summary
        click.echo(f"  chunks       : {s.chunks} ({s.cached_chunks} cached)")
        click.echo(f"  failed_chunks: {s.failed_chunks}")
        click.echo(f"  cost_usd     : ${s.cost_usd:.4f}")
    else:
        click.echo("  cost_usd     : (extractor did not run — see logs)")
    click.echo(f"  source_id    : {summary.source_id}")


@lore_ingest_cmd.command(name="target")
@click.argument("lore_dir", type=_LORE_DIR_TYPE)
@click.argument(
    "epub_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
)
@click.option(
    "--helper-model",
    "helper_model",
    default=None,
    help=(
        "Helper model. Defaults to $EPUBLATE_LLM_HELPER_MODEL or the translator model."
    ),
)
@click.option(
    "--max-chapters",
    "max_chapters",
    type=click.IntRange(min=1),
    default=None,
    help="Cap on chapters scanned for target-only proposals.",
)
@click.pass_context
def lore_ingest_target(
    ctx: click.Context,
    lore_dir: Path,
    epub_path: Path,
    helper_model: str | None,
    max_chapters: int | None,
) -> None:
    """Ingest a *target-language* ePub into ``LORE_DIR``.

    Useful for series whose first books are already translated: the
    helper LLM proposes canonical target spellings (no source term)
    that the translator can then anchor to in later books (PRD F-LB-3).
    """

    from epublate.llm.factory import ENV_MODEL, build_provider, resolve_helper_model
    from epublate.lore import LoreBook, ingest_target_epub
    from epublate.lore.ingest_target import DEFAULT_TARGET_MAX_CHAPTERS

    use_mock = bool(ctx.obj.get("mock_llm"))
    translator_model = os.environ.get(ENV_MODEL)
    chosen_helper = resolve_helper_model(translator_model, override=helper_model)
    provider = build_provider(mock=use_mock)

    book = LoreBook.open(lore_dir)
    try:
        source_row, summary = ingest_target_epub(
            book,
            epub_path=epub_path,
            provider=provider,
            helper_model=chosen_helper,
            max_chapters=max_chapters or DEFAULT_TARGET_MAX_CHAPTERS,
        )
    finally:
        book.close()

    click.echo("Target ingest complete")
    click.echo(f"  proposed     : {summary.proposed_count}")
    click.echo(f"  chunks       : {summary.chunks} ({summary.cached_chunks} cached)")
    click.echo(f"  failed_chunks: {summary.failed_chunks}")
    click.echo(f"  cost_usd     : ${summary.cost_usd:.4f}")
    click.echo(f"  source_id    : {source_row.id}")
    if summary.notes:
        click.echo("  notes        :")
        for note in summary.notes:
            click.echo(f"    - {note}")


@lore_cmd.command(name="export")
@click.argument("lore_dir", type=_LORE_DIR_TYPE)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path, resolve_path=True),
    required=True,
    help="Destination JSON file (will be overwritten).",
)
def lore_export(lore_dir: Path, out_path: Path) -> None:
    """Export a Lore Book's glossary to JSON (format v2)."""

    from epublate.glossary import io as glossary_io
    from epublate.lore import LoreBook

    book = LoreBook.open(lore_dir)
    try:
        payload = glossary_io.export_json(book.engine, book.project_id)
    finally:
        book.close()
    glossary_io.write_export(out_path, payload)
    click.echo(f"Exported {len(payload['entries'])} entries to {out_path}")


@lore_cmd.command(name="import")
@click.argument("lore_dir", type=_LORE_DIR_TYPE)
@click.argument(
    "in_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
)
@click.option(
    "--conflict",
    "conflict",
    type=click.Choice(["skip", "overwrite"]),
    default="skip",
    show_default=True,
    help="Behavior when an entry with the same source_term already exists.",
)
def lore_import(lore_dir: Path, in_path: Path, conflict: str) -> None:
    """Import a glossary JSON file into a Lore Book.

    Accepts both v1 and v2 formats; v1 entries are upgraded with
    ``source_known=True`` (PRD §4.3 / F-LB-10).
    """

    from epublate.glossary import io as glossary_io
    from epublate.lore import LoreBook

    payload = glossary_io.read_payload(in_path)
    book = LoreBook.open(lore_dir)
    try:
        summary = glossary_io.import_json(
            book.engine,
            project_id=book.project_id,
            payload=payload,
            conflict=conflict,  # type: ignore[arg-type]
        )
    finally:
        book.close()
    click.echo(
        f"Imported {summary.created} created, "
        f"{summary.updated} updated, "
        f"{summary.skipped} skipped"
    )


@lore_cmd.command(name="import-project")
@click.argument("lore_dir", type=_LORE_DIR_TYPE)
@click.argument(
    "project_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path, resolve_path=True),
)
@click.option(
    "--on-conflict",
    "on_conflict",
    type=click.Choice(["skip", "overwrite"]),
    default="skip",
    show_default=True,
    help=(
        "What to do when an entry already exists in the Lore Book "
        "with the same source_term + type. Use the TUI for interactive "
        "per-conflict resolution."
    ),
)
def lore_import_project(lore_dir: Path, project_dir: Path, on_conflict: str) -> None:
    """Import a translation project's curated glossary into a Lore Book.

    The destination Lore Book accumulates source/target term mappings
    (and aliases, gender, notes) from the project's glossary so the
    next book in the series can attach the Lore Book and reuse the
    canonical translations. The source project is opened read-only and
    closed before this command returns.
    """

    from epublate.lore import LoreBook, import_project_glossary

    book = LoreBook.open(lore_dir)
    try:
        summary = import_project_glossary(
            book,
            src_project_dir=project_dir,
            policy=on_conflict,  # type: ignore[arg-type]
        )
    finally:
        book.close()
    click.echo(
        f"Imported from {project_dir}:\n"
        f"  created   : {summary.created}"
        + (
            f" (target-only: {summary.target_only_inserts})"
            if summary.target_only_inserts
            else ""
        )
        + f"\n  updated   : {summary.updated}\n"
        f"  skipped   : {summary.skipped}"
    )


def _slugify_lore_name(text: str) -> str:
    cleaned: list[str] = []
    last_dash = False
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
            last_dash = False
        elif not last_dash and char in (" ", "-", "_"):
            cleaned.append("-")
            last_dash = True
    return "".join(cleaned).strip("-")


__all__ = ["main"]
