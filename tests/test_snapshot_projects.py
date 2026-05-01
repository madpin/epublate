"""Snapshot baseline for the modern Projects landing screen (PRD §4.6)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from epublate.app.main import EpublateApp
from epublate.app.recents import RecentProject, RecentsStore
from epublate.app.screens.projects import ProjectsScreen
from tests._snapshot_helpers import PilotLike

TERMINAL_SIZE: tuple[int, int] = (120, 36)


def _populate_recents(path: Path) -> None:
    store = RecentsStore()
    base_ts = 1_700_000_000.0  # frozen timestamp keeps the SVG stable
    for i, (name, src, tgt) in enumerate(
        (
            ("Pride and Prejudice", "en", "pt"),
            ("The Time Machine", "en", "es"),
            ("Le Petit Prince", "fr", "en"),
        )
    ):
        # The Projects screen auto-prunes entries whose folders have
        # vanished on mount. Make the mock project directories real so
        # they survive the mount-time sweep; the mask callback still
        # rewrites the visible cells before the snapshot.
        slug = name.lower().replace(" ", "-")
        project_dir = path.parent / f"<{slug}>"
        project_dir.mkdir(parents=True, exist_ok=True)
        store.upsert(
            RecentProject(
                project_dir=str(project_dir),
                name=name,
                source_lang=src,
                target_lang=tgt,
                last_opened=base_ts - i * 3600,
            )
        )
    store.save(path)


def _mask_projects_table(
    pilot: PilotLike,
) -> Callable[[], Awaitable[None]] | None:
    async def _run() -> None:
        await pilot.pause()
        screen = pilot.app.screen
        if not isinstance(screen, ProjectsScreen):
            return
        from textual.widgets import DataTable, Static

        table = screen.query_one("#projects-table", DataTable)
        table.clear()
        rows = [
            (
                "[yellow]●[/]",
                "[b]Pride and Prejudice[/]",
                "[cyan]en[/] → [yellow]pt[/]",
                "[cyan]████░░░░░░░░[/] [b] 42.0%[/]",
                "[dim]2d ago[/]",
                "[dim]<projects>/pride[/]",
            ),
            (
                "[yellow]●[/]",
                "[b]The Time Machine[/]",
                "[cyan]en[/] → [yellow]es[/]",
                "[dim]█░░░░░░░░░░░[/] [b]  5.5%[/]",
                "[dim]3d ago[/]",
                "[dim]<projects>/wells[/]",
            ),
            (
                "[green]★[/]",
                "[b]Le Petit Prince[/]",
                "[cyan]fr[/] → [yellow]en[/]",
                "[green]████████████[/] [b]100.0%[/]",
                "[dim]1w ago[/]",
                "[dim]<projects>/saint-ex[/]",
            ),
        ]
        for row in rows:
            table.add_row(*row)
        screen.query_one("#projects-summary", Static).update(
            "  [yellow][b]3[/][/] recent projects   [dim]·[/]   "
            "[green]2 ready[/]  [red]1 missing[/]   [dim]·[/]   "
            "[dim]config: ~/.config/epublate/recents.json[/]"
        )
        screen.query_one("#projects-status", Static).update(
            "[dim] Ready  ·  press [/][b]?[/]"
            "[dim] for help, [/][b]T[/][dim] to switch theme[/]"
        )
        await pilot.pause()

    return _run()


def test_snapshot_projects(
    snap_compare: Callable[..., bool],
    tmp_path: Path,
) -> None:
    recents = tmp_path / "recents.json"
    _populate_recents(recents)
    screen = ProjectsScreen(recents_path=recents)
    app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
    assert snap_compare(
        app,
        terminal_size=TERMINAL_SIZE,
        run_before=_mask_projects_table,
    )
