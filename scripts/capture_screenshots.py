"""Capture documentation screenshots for every Textual screen.

This is a developer utility, not part of the application surface. It
walks the same screen taxonomy the snapshot tests cover (PRD §4.6) and
writes one SVG + one PNG per screen into ``docs/screenshots/``. The
docs (README + ``docs/USAGE.md``) embed those filenames directly.

Why a script instead of reusing the ``tests/__snapshots__/*.raw``
files: the snapshot fixtures are intentionally minimal — they exist to
detect regressions, not to look pretty. The captures here use richer
fixtures (multi-chapter book, populated glossary, finished
translation) so the docs show the app in a state that resembles real
curator usage.

Run with::

    uv run python scripts/capture_screenshots.py

Optional flags::

    --only PATTERN   only render captures whose name matches PATTERN
    --no-png         skip PNG rasterization (SVG-only)
    --out DIR        write to a custom output directory

The PNG path uses ``resvg-py`` (pure-Rust SVG renderer, no system
libraries required); install it with ``uv pip install resvg-py`` if
the script complains. Without it, the script still emits SVGs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from ebooklib import epub

# Make ``src/`` importable when the script is run before ``uv sync``
# wires the package into the venv.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from epublate.app.config import UIConfig  # noqa: E402
from epublate.app.main import EpublateApp  # noqa: E402
from epublate.app.recents import RecentProject, RecentsStore  # noqa: E402
from epublate.app.screens.dashboard import DashboardScreen  # noqa: E402
from epublate.app.screens.glossary import GlossaryScreen  # noqa: E402
from epublate.app.screens.inbox import InboxScreen  # noqa: E402
from epublate.app.screens.projects import ProjectsScreen  # noqa: E402
from epublate.app.screens.reader import ReaderScreen  # noqa: E402
from epublate.app.screens.settings import SettingsScreen  # noqa: E402
from epublate.app.themes import (  # noqa: E402
    EPUBLATE_CONTRAST_THEME_NAME,
    EPUBLATE_DEFAULT_THEME_NAME,
)
from epublate.core.project import Project  # noqa: E402
from epublate.db import repo  # noqa: E402
from epublate.llm.factory import (  # noqa: E402
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_HELPER_MODEL,
    ENV_MODEL,
    ENV_ORG,
    ENV_PROVIDER,
)
from epublate.llm.mock import MockLLMProvider  # noqa: E402

_logger = logging.getLogger("capture_screenshots")

TERMINAL_SIZE: tuple[int, int] = (120, 36)
DEFAULT_OUT = _REPO_ROOT / "docs" / "screenshots"


# ---------------------------------------------------------------------------
# Fixture helpers (mirror tests/conftest.py and tests/_snapshot_helpers.py).
# ---------------------------------------------------------------------------

XHTML_TEMPLATE = (
    "<?xml version='1.0' encoding='utf-8'?>"
    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">'
    "<head><title>{title}</title></head><body>{body}</body></html>"
)

DEMO_CHAPTERS: tuple[tuple[str, str], ...] = (
    (
        "Chapter One — The Citadel",
        "<h1>Chapter One</h1>"
        "<p>The brave hero <em>Élise</em> sailed for the lost citadel.</p>"
        "<p>Behind them, the city of <strong>Marenglade</strong> slept.</p>"
        "<p>A storm gathered over the bay; gulls wheeled in silence.</p>",
    ),
    (
        "Chapter Two — The Crossing",
        "<h1>Chapter Two</h1>"
        "<p>By dawn the citadel rose from the mist like an old promise.</p>"
        "<p>Élise tightened her grip on the wheel and whispered a name.</p>",
    ),
)


def _make_epub(out_path: Path) -> Path:
    """Build a tiny well-formed ePub for fixture work."""

    book = epub.EpubBook()
    book.set_identifier("urn:epublate:docs:capture")
    book.set_title("The Citadel — A Demonstration")
    book.set_language("en")
    book.add_author("epublate docs fixture")

    items: list[epub.EpubHtml] = []
    for idx, (chap_title, body) in enumerate(DEMO_CHAPTERS, start=1):
        uid = f"ch{idx:02d}"
        item = epub.EpubHtml(
            uid=uid,
            file_name=f"{uid}.xhtml",
            lang="en",
            title=chap_title,
        )
        item.content = XHTML_TEMPLATE.format(
            lang="en", title=chap_title, body=body
        ).encode("utf-8")
        book.add_item(item)
        items.append(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *items]
    book.toc = [epub.Link(it.file_name, it.title, it.id) for it in items]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def _make_project(workspace: Path) -> Project:
    src = _make_epub(workspace / "source.epub")
    return Project.create(
        src,
        out_dir=workspace / "the-citadel",
        source_lang="en",
        target_lang="pt",
    )


def _seed_glossary(project: Project) -> None:
    repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        type="character",
        source_term="Élise",
        target_term="Élise",
        status="locked",
        notes="Protagonist; spelling preserved across editions.",
    )
    repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        type="place",
        source_term="Marenglade",
        target_term="Marenglade",
        status="confirmed",
        notes="Coastal city; canonical Portuguese keeps the original spelling.",
    )
    repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        type="place",
        source_term="the citadel",
        target_term="a cidadela",
        status="proposed",
    )


def _seed_recents(path: Path, scratch_root: Path) -> None:
    """Populate a fake recents.json so the Projects screen has rows."""

    store = RecentsStore()
    base_ts = 1_716_140_000.0  # frozen-ish so the "ago" labels stay stable
    for offset, (name, src, tgt) in enumerate(
        (
            ("The Citadel", "en", "pt"),
            ("Pride and Prejudice", "en", "pt-BR"),
            ("Le Petit Prince", "fr", "en"),
        )
    ):
        slug = name.lower().replace(" ", "-")
        project_dir = scratch_root / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        store.upsert(
            RecentProject(
                project_dir=str(project_dir),
                name=name,
                source_lang=src,
                target_lang=tgt,
                last_opened=base_ts - offset * 3600 * 24,
            )
        )
    store.save(path)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _set_theme(app: EpublateApp, name: str) -> None:
    """Switch theme without going through the cycle keybinding."""

    if name in app.available_themes:
        app.theme = name


async def _capture(
    app: EpublateApp,
    *,
    output: Path,
    presses: tuple[str, ...] = (),
    after_mount: Callable[[Any], Awaitable[None]] | None = None,
    theme: str | None = None,
) -> str:
    """Mount ``app`` headlessly, perform the prep steps, return the SVG."""

    async with app.run_test(size=TERMINAL_SIZE) as pilot:
        await pilot.pause()
        if theme is not None:
            _set_theme(pilot.app, theme)
            await pilot.pause()
        # Apply any masking *before* pressing keys, so masks targeting the
        # base screen run while it's still on top of the screen stack —
        # otherwise pushing a modal hides our intended target from
        # ``screen.query_one`` lookups.
        if after_mount is not None:
            await after_mount(pilot)
            await pilot.pause()
        for key in presses:
            await pilot.press(key)
            await pilot.pause()
        # ``simplify=False`` keeps the chrome (window controls + title)
        # which makes the rendered images feel like real screenshots.
        svg = pilot.app.export_screenshot(simplify=False)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    return svg


def _svg_to_png(svg_path: Path, png_path: Path, *, width: int = 1480) -> bool:
    try:
        import resvg_py  # type: ignore[import-not-found]
    except ImportError:
        _logger.warning(
            "resvg-py is not installed; skipping PNG for %s. "
            "Install with `uv pip install resvg-py` to enable PNG output.",
            png_path.name,
        )
        return False
    data = resvg_py.svg_to_bytes(svg_path=str(svg_path), width=width)
    png_path.write_bytes(bytes(data))
    return True


# ---------------------------------------------------------------------------
# Per-screen build/mask plumbing
# ---------------------------------------------------------------------------


def _mask_dashboard_project(replacement: str) -> Callable[[Any], Awaitable[None]]:
    async def _run(pilot: Any) -> None:
        await pilot.pause()
        screen = pilot.app.screen
        try:
            widget = screen.query_one("#dashboard-project")
        except Exception:
            return
        widget.update(replacement)
        await pilot.pause()

    return _run


_DASHBOARD_PROJECT_BLOCK = (
    "[b]The Citadel[/b]  (en → pt)\n"
    "  source: ~/Documents/epublate/the-citadel/original.epub"
)


def _mask_projects_table(pilot: Any) -> Awaitable[None]:
    """Replace the Projects table + summary with a polished demo state."""

    async def _run() -> None:
        await pilot.pause()
        screen = pilot.app.screen
        if not isinstance(screen, ProjectsScreen):
            return
        from textual.widgets import DataTable, Static

        table = screen.query_one("#projects-table", DataTable)
        table.clear()
        rows = (
            (
                "[yellow]●[/]",
                "[b]The Citadel[/]",
                "[cyan]en[/] → [yellow]pt[/]",
                "[cyan]████░░░░░░░░[/] [b] 42.0%[/]",
                "[dim]2h ago[/]",
                "[dim]~/Documents/epublate/the-citadel[/]",
            ),
            (
                "[yellow]●[/]",
                "[b]Pride and Prejudice[/]",
                "[cyan]en[/] → [yellow]pt-BR[/]",
                "[dim]█░░░░░░░░░░░[/] [b]  5.5%[/]",
                "[dim]3d ago[/]",
                "[dim]~/Documents/epublate/pride-and-prejudice[/]",
            ),
            (
                "[green]★[/]",
                "[b]Le Petit Prince[/]",
                "[cyan]fr[/] → [yellow]en[/]",
                "[green]████████████[/] [b]100.0%[/]",
                "[dim]1w ago[/]",
                "[dim]~/Documents/epublate/le-petit-prince[/]",
            ),
        )
        for row in rows:
            table.add_row(*row)
        screen.query_one("#projects-summary", Static).update(
            "  [yellow][b]3[/][/] recent projects   [dim]·[/]   "
            "[green]2 ready[/]  [yellow]1 in progress[/]   [dim]·[/]   "
            "[dim]new projects land in[/] [b]~/Documents/epublate[/]"
        )
        screen.query_one("#projects-status", Static).update(
            "[dim] Ready  ·  press [/][b]?[/]"
            "[dim] for help, [/][b]T[/][dim] to switch theme[/]"
        )
        await pilot.pause()

    return _run()


def _mask_settings_paths(pilot: Any) -> Awaitable[None]:
    async def _run() -> None:
        await pilot.pause()
        screen = pilot.app.screen
        replacements = {
            "settings-project-body": (
                "  name        : The Citadel\n"
                "  source lang : en\n"
                "  target lang : pt\n"
                "  source epub : ~/Documents/epublate/the-citadel/original.epub\n"
                "  database    : ~/Documents/epublate/the-citadel"
                "/the-citadel.epublate\n"
                "  budget cap  : (none)"
            ),
            "settings-style-body": (
                "  preset  : Literary fiction\n"
                "  preview : Translate as adult literary fiction. "
                "Preserve narrator's voice, rhythm, and subtext.\n"
                "  size    : 600 chars in the system prompt"
            ),
            "settings-ui-body": (
                "  active theme    : epublate\n"
                "  saved theme     : epublate\n"
                "  config file     : ~/.config/epublate/ui.toml\n"
                "  cycle order     : epublate, textual-dark, textual-light, "
                "epublate-contrast\n"
                "  auto tone-sniff : on"
            ),
        }
        for widget_id, replacement in replacements.items():
            try:
                widget = screen.query_one(f"#{widget_id}")
            except Exception:
                continue
            widget.update(replacement)
        await pilot.pause()

    return _run()


# ---------------------------------------------------------------------------
# Capture catalogue
# ---------------------------------------------------------------------------


def _projects_screen_app(workspace: Path) -> EpublateApp:
    recents = workspace / "recents.json"
    _seed_recents(recents, workspace / "fake-projects")
    screen = ProjectsScreen(recents_path=recents)
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _dashboard_app(project: Project, workspace: Path) -> EpublateApp:
    screen = DashboardScreen(
        project,
        provider_factory=MockLLMProvider,
        default_model="gpt-mock",
    )
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _reader_app(project: Project, workspace: Path) -> EpublateApp:
    screen = ReaderScreen(project, provider_factory=MockLLMProvider)
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _glossary_app(project: Project, workspace: Path) -> EpublateApp:
    screen = GlossaryScreen(project)
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _inbox_app(project: Project, workspace: Path) -> EpublateApp:
    screen = InboxScreen(project, provider_factory=MockLLMProvider)
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _settings_app(project: Project, workspace: Path) -> EpublateApp:
    screen = SettingsScreen(
        project,
        ui_config=UIConfig(theme=EPUBLATE_DEFAULT_THEME_NAME),
        default_model="gpt-5-mini",
    )
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _new_project_modal_app(workspace: Path) -> EpublateApp:
    """Mount the Projects screen and pop the New Project modal on top."""

    recents = workspace / "recents.json"
    _seed_recents(recents, workspace / "fake-projects")
    screen = ProjectsScreen(recents_path=recents)
    return EpublateApp(initial_screen=screen, config_path=workspace / "ui.toml")


def _open_project_modal_app(workspace: Path) -> EpublateApp:
    return _new_project_modal_app(workspace)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _isolated_environment() -> Iterator[Path]:
    """Quarantine the script from the user's real ~/.config/epublate.

    Mirrors the autouse ``_xdg_isolation`` fixture in ``tests/conftest.py``:
    we redirect XDG paths and the projects root to a tmp dir so running
    the script never touches the developer's recents list. Also pins the
    LLM env vars so the Settings capture is deterministic.
    """

    with tempfile.TemporaryDirectory(prefix="epublate-screens-") as raw:
        root = Path(raw)
        config_home = root / "xdg-config"
        data_home = root / "xdg-data"
        projects_root = root / "projects"
        for d in (config_home, data_home, projects_root):
            d.mkdir(parents=True, exist_ok=True)

        original = {
            "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
            "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
            "EPUBLATE_PROJECTS_ROOT": os.environ.get("EPUBLATE_PROJECTS_ROOT"),
            ENV_PROVIDER: os.environ.get(ENV_PROVIDER),
            ENV_BASE_URL: os.environ.get(ENV_BASE_URL),
            ENV_API_KEY: os.environ.get(ENV_API_KEY),
            ENV_MODEL: os.environ.get(ENV_MODEL),
            ENV_HELPER_MODEL: os.environ.get(ENV_HELPER_MODEL),
            ENV_ORG: os.environ.get(ENV_ORG),
        }

        os.environ["XDG_CONFIG_HOME"] = str(config_home)
        os.environ["XDG_DATA_HOME"] = str(data_home)
        os.environ["EPUBLATE_PROJECTS_ROOT"] = str(projects_root)
        os.environ[ENV_PROVIDER] = "openai-compat"
        os.environ[ENV_BASE_URL] = "https://api.example.com/v1"
        os.environ[ENV_API_KEY] = "sk-docs-screenshot-1234567890"
        os.environ[ENV_MODEL] = "gpt-5-mini"
        os.environ[ENV_HELPER_MODEL] = "gpt-5-mini"
        os.environ.pop(ENV_ORG, None)

        try:
            yield root
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


async def _run_all(out_dir: Path, *, only: str | None, write_png: bool) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    with _isolated_environment() as root:
        # One reusable project + glossary for all project-scoped screens.
        project = _make_project(root)
        _seed_glossary(project)

        try:
            captures: list[tuple[str, Callable[[], Awaitable[Path]]]] = []

            def register(
                name: str,
                builder: Callable[[], EpublateApp],
                *,
                presses: tuple[str, ...] = (),
                after_mount: Callable[[Any], Awaitable[None]] | None = None,
                theme: str | None = None,
            ) -> None:
                async def _do() -> Path:
                    target = out_dir / f"{name}.svg"
                    await _capture(
                        builder(),
                        output=target,
                        presses=presses,
                        after_mount=after_mount,
                        theme=theme,
                    )
                    return target

                captures.append((name, _do))

            register(
                "01-projects",
                lambda: _projects_screen_app(root),
                after_mount=lambda pilot: _mask_projects_table(pilot),
            )
            register(
                "02-new-project",
                lambda: _new_project_modal_app(root),
                presses=("n",),
            )
            register(
                "03-open-project",
                lambda: _open_project_modal_app(root),
                presses=("o",),
            )
            register(
                "04-dashboard",
                lambda: _dashboard_app(project, root),
                after_mount=_mask_dashboard_project(_DASHBOARD_PROJECT_BLOCK),
            )
            register(
                "05-reader",
                lambda: _reader_app(project, root),
            )
            register(
                "06-glossary",
                lambda: _glossary_app(project, root),
            )
            register(
                "07-inbox",
                lambda: _inbox_app(project, root),
            )
            register(
                "08-settings",
                lambda: _settings_app(project, root),
                after_mount=lambda pilot: _mask_settings_paths(pilot),
            )
            register(
                "09-help",
                lambda: _dashboard_app(project, root),
                presses=("question_mark",),
                after_mount=_mask_dashboard_project(_DASHBOARD_PROJECT_BLOCK),
            )
            register(
                "10-dashboard-theme-textual-dark",
                lambda: _dashboard_app(project, root),
                after_mount=_mask_dashboard_project(_DASHBOARD_PROJECT_BLOCK),
                theme="textual-dark",
            )
            register(
                "11-dashboard-theme-textual-light",
                lambda: _dashboard_app(project, root),
                after_mount=_mask_dashboard_project(_DASHBOARD_PROJECT_BLOCK),
                theme="textual-light",
            )
            register(
                "12-dashboard-theme-contrast",
                lambda: _dashboard_app(project, root),
                after_mount=_mask_dashboard_project(_DASHBOARD_PROJECT_BLOCK),
                theme=EPUBLATE_CONTRAST_THEME_NAME,
            )

            for name, runner in captures:
                if only is not None and only not in name:
                    continue
                _logger.info("capture %s", name)
                svg_path = await runner()
                if write_png:
                    png_path = svg_path.with_suffix(".png")
                    if _svg_to_png(svg_path, png_path):
                        _logger.info(" wrote %s", png_path.relative_to(_REPO_ROOT))
                _logger.info(" wrote %s", svg_path.relative_to(_REPO_ROOT))
                written += 1
        finally:
            project.close()

    return written


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render documentation screenshots for every Textual screen."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output directory (default: {DEFAULT_OUT.relative_to(_REPO_ROOT)})",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="only render captures whose name contains this substring",
    )
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="skip PNG rasterization (SVG-only output)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    written = asyncio.run(
        _run_all(
            out_dir=args.out,
            only=args.only,
            write_png=not args.no_png,
        )
    )
    if written == 0:
        _logger.error("no captures matched --only=%s", args.only)
        return 1
    _logger.info("done — %d capture(s) written to %s", written, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
