"""`secret-rotator ui`'s one filter (design §7.5, §7.7, R89): `f` narrows the list to the selected
box's type — a manual key's credential type, any other kind's name — and `f` again shows every box;
the footer's label counts what `f` keeps and never names the type; a filtered list keeps its order
through a done box, and gone empty says a filter is applied; nothing but `f` clears it."""

from plans import fake_of, put_state
from test_listing import (
    BOT,
    HOOK,
    IAC_AGENT,
    MIXED,
    OPENBAO_ADMIN,
    PAT,
    SEAL,
    SHOP,
    WIFI,
    YEARLY,
    leaf,
    seed_store,
    store,
)
from test_ui import (
    FAILED,
    IN_FLIGHT,
    ORDER,
    SIZE,
    WAITING,
    app_of,
    in_box,
    select,
    settled,
    styles,
    until,
    world,
)
from textual.widgets import Static

from secret_rotator.ui.app import FOOTER
from secret_rotator.ui.widgets import GREYED, Box, EmptyState, FilterNotice

GIT = "rotator/bootstrap/git-api-token"  # external, never stamped
PAT2 = "eso/prd/gh2/prd/pat"  # manual, a GitHub personal access token, yearly
MANUAL = [FAILED, f"{BOT}#telegram-bot-token", WAITING, f"{SHOP}#api-key", f"{WIFI}#password"]


def footer(app):
    return app.query_one("#footer", Static).content


def label(app) -> str:
    """The filter's label, between the footer's two parts."""
    return footer(app).plain.removeprefix(FOOTER[0]).removesuffix(FOOTER[1])


def shown(app) -> list[str]:
    return [box.item.id for box in app.query(Box) if box.display]


def visible(app) -> list[type]:
    return [type(w) for w in app.box_list.children if w.display]


def bundled():
    """The list's world with another external key and another GitHub token."""
    bao = world()
    extra = fake_of(
        {GIT: seed_store()[GIT]}
        | {PAT2: leaf(PAT2, token={"kind": "manual", "args": {"type": "github-pat"}, **YEARLY})}
    )
    bao.leaves |= extra.leaves
    put_state(bao, PAT2, stamps={"token": "2025-09-01"})
    return bao


async def done(pilot, app, rid):
    """Opens the selected external box and presses its Done; the box leaves."""
    await pilot.press("enter")
    await until(pilot, lambda: in_box(app))
    await pilot.press("enter")
    await until(pilot, lambda: rid not in app.order and rid not in shown(app))


async def test_f_narrows_the_list_to_the_selected_box_s_type_and_f_again_shows_every_box():
    app = app_of(world())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert footer(app).plain == (
            " ↑↓ select · ⏎ open · Esc back · f filter · 5 similar · ? help · q quit"
        )
        await pilot.press("f")  # FAILED: a manual key without a credential type
        assert shown(app) == app.shown() == MANUAL and app.selected == FAILED
        assert label(app) == "f clear filter"
        await pilot.press("down")
        assert app.selected == MANUAL[1]  # the approle box between them is not shown
        await pilot.press("end")
        assert app.selected == MANUAL[-1]
        await pilot.press("home", "down", "down")
        assert app.selected == WAITING
        await pilot.press("f")
        assert shown(app) == ORDER and app.selected == WAITING
        assert label(app) == "f filter · 5 similar"
        await pilot.press("down")
        assert app.selected == f"{PAT}#token"


async def test_the_label_counts_what_f_keeps_and_is_greyed_for_a_box_without_others():
    app = app_of(world())
    bells = []
    app.bell = lambda: bells.append(1)
    async with app.run_test(size=SIZE) as pilot:
        await select(pilot, app, f"{IAC_AGENT}#secret_id")
        assert label(app) == "f filter · 2 similar"
        assert "f filter · 2 similar" not in styles(footer(app))  # the footer's own colour
        await select(pilot, app, f"{PAT}#token")  # the one GitHub token
        assert label(app) == "f filter · 1 item"
        assert styles(footer(app))["f filter · 1 item"] == GREYED
        await pilot.press("f")
        await settled(pilot)
        assert bells == [1] and app.filtered is None and shown(app) == ORDER


async def test_approle_and_external_boxes_are_filtered_by_their_kind_beside_the_manual_ones():
    """A5: approle's manual deliveries are one type, the external keys another; a manual key is
    filtered by its credential type, one without a type by its kind. The label never names the
    type."""
    app = app_of(bundled())
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        for rid, kept in [
            (IN_FLIGHT, [IN_FLIGHT, f"{IAC_AGENT}#secret_id"]),
            (f"{SEAL}#seal-key", [f"{GIT}#git-api-token", f"{SEAL}#seal-key"]),
            (f"{PAT}#token", [f"{PAT}#token", f"{PAT2}#token"]),  # due the same day: by leaf
            (f"{BOT}#telegram-bot-token", MANUAL),
        ]:
            await select(pilot, app, rid)
            assert label(app) == f"f filter · {len(kept)} similar", rid
            await pilot.press("f")
            assert shown(app) == kept, rid
            assert not {"approle", "external", "github-pat", "manual"} & set(label(app).split())
            await pilot.press("f")
            assert len(shown(app)) == len(app.order) == len(ORDER) + 2


async def test_a_filter_holds_after_a_box_is_done_and_an_emptied_list_says_a_filter_is_applied():
    """Only the never-stamped external key waits: done, the green box tops the list; the
    filtered list's last done, the list says a filter is applied under it."""
    bao = fake_of(store() | {GIT: seed_store()[GIT]})
    yesterday = "2026-10-07"
    for path, key in [
        (PAT, "token"),
        (BOT, "telegram-bot-token"),
        (HOOK, "secret"),
        (IAC_AGENT, "secret_id"),
        (OPENBAO_ADMIN, "secret_id"),
        (SEAL, "seal-key"),
    ]:
        put_state(bao, path, stamps={key: yesterday})
    put_state(bao, MIXED, stamps={"password": yesterday, "pin": yesterday})
    app = app_of(bao)
    externals = [f"{GIT}#git-api-token", f"{SEAL}#seal-key"]
    async with app.run_test(size=SIZE) as pilot:
        await settled(pilot)
        assert app.selected == externals[0] and app.order.index(externals[1]) > 1
        await pilot.press("f")
        assert shown(app) == externals
        await done(pilot, app, externals[0])
        # the box below it in the filtered list, not in the whole list
        assert app.selected == externals[1] and app.filtered == "external"
        await until(pilot, lambda: bool(app.query(EmptyState)))
        assert shown(app) == [externals[1]] and label(app) == "f clear filter"
        await done(pilot, app, externals[1])
        await until(pilot, lambda: bool(app.query(FilterNotice)))
        assert visible(app) == [EmptyState, FilterNotice]  # the green box over the notice
        notice = app.query_one(FilterNotice).content.plain
        assert notice == "A filter is applied — f shows every box again."
        assert app.selected is None and app.filtered == "external"
        assert label(app) == "f clear filter"
        await pilot.press("down", "end", "enter")
        await settled(pilot)
        assert app.filtered == "external" and not shown(app) and app.focused is None
        await pilot.press("f")
        await until(pilot, lambda: not app.query(FilterNotice))
        assert app.filtered is None and shown(app) == app.order
        assert app.selected == app.order[0] and app.box(app.selected).has_class("-selected")
        assert visible(app)[0] is EmptyState
