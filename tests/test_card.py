"""The standing card (design §3.3): the YouTrack client's search, creation, comment and rewrite, and
the card's list — created with the first item, commented with what changed, untouched when nothing
did."""

import datetime

import pytest
from fake_youtrack import TAG, TOKEN, FakeYouTrack

from secret_rotator import card, youtrack
from secret_rotator.card import Section
from secret_rotator.youtrack import YouTrack, YouTrackError

TODAY = datetime.date(2026, 10, 5)
A, B, C = "`eso/a`: rotation_activate: missing", "`eso/b`: failed", "`eso/c`: skipped"


def client(fake):
    return YouTrack(TOKEN, opener=fake)


def post(fake, *items, dry_run=False, today=TODAY):
    return card.post(
        client(fake),
        TAG,
        client(fake).open_card(TAG),
        [Section("Findings", items)],
        today=today,
        dry_run=dry_run,
    )


class TestTheClient:
    def test_the_open_card_is_the_oldest_unresolved_issue_with_the_tag(self):
        fake = FakeYouTrack()
        assert client(fake).open_card(TAG) is None
        closed = fake.card("old")
        closed["resolved"] = True
        fake.card("first")
        fake.card("second")
        found = client(fake).open_card(TAG)
        assert (found.readable, found.description) == ("ANS-102", "first")

    def test_a_card_is_created_through_every_page_of_projects_and_tags(self, monkeypatch):
        monkeypatch.setattr(youtrack, "PAGE", 1)
        fake = FakeYouTrack(tags=("Operator Action", "Later", TAG))
        made = client(fake).create_card(TAG, "s", "d")
        assert made.readable == "ANS-101"
        assert fake.issues[0]["tags"] == [TAG]
        assert fake.issues[0]["customFields"] == {"Type": "Task", "State": "New"}

    def test_a_tag_its_token_does_not_see_is_named(self):
        with pytest.raises(
            YouTrackError, match="YouTrack shows its token no tag Rotator Standing Card"
        ):
            client(FakeYouTrack(tags=("Other",))).create_card(TAG, "s", "d")

    def test_a_refusal_names_its_status_and_never_the_token(self):
        with pytest.raises(YouTrackError) as e:
            YouTrack("SECRET-wrong", opener=FakeYouTrack()).open_card(TAG)
        assert str(e.value) == "GET /api/issues: HTTP 401: Unauthorized"
        assert "SECRET" not in str(e.value)

    def test_a_transport_failure_is_named(self):
        fake = FakeYouTrack()
        fake.down = True
        with pytest.raises(YouTrackError, match="transport error"):
            client(fake).open_card(TAG)


class TestTheList:
    def test_nothing_open_and_no_card_makes_none(self):
        fake = FakeYouTrack()
        assert post(fake) == (None, "no card: nothing is open")
        assert fake.writes() == []

    def test_the_first_item_creates_the_card(self):
        fake = FakeYouTrack()
        posted, what = post(fake, A, B)
        assert what == "ANS-101: created with 2 item(s)"
        description = fake.issues[0]["description"]
        assert description.startswith("Open as of 2026-10-05.\n\n")
        assert f"### Findings\n- {A}\n- {B}" in description
        assert card.items_of(description) == [A, B]

    def test_a_change_rewrites_the_list_and_comments_what_changed(self):
        fake = FakeYouTrack()
        post(fake, A, B)
        _, what = post(fake, B, C, today=TODAY + datetime.timedelta(days=1))
        assert what == "ANS-101: 1 new, 1 resolved"
        issue = fake.issues[0]
        assert card.items_of(issue["description"]) == [B, C]
        assert issue["comments"] == [
            f"2026-10-06: 1 new, 1 resolved.\n\nNew:\n- {C}\n\nResolved:\n- {A}"
        ]

    def test_the_same_list_touches_nothing(self):
        fake = FakeYouTrack()
        post(fake, A, B)
        writes = len(fake.writes())
        assert post(fake, B, A, today=TODAY + datetime.timedelta(days=1))[1] == (
            "ANS-101: nothing changed"
        )
        assert len(fake.writes()) == writes

    def test_the_run_turning_live_rewrites_the_dry_run_mark(self):
        fake = FakeYouTrack()
        post(fake, A, dry_run=True)
        assert card.is_dry_run(fake.issues[0]["description"])
        post(fake, A)
        assert not card.is_dry_run(fake.issues[0]["description"])
        assert fake.issues[0]["comments"] == ["2026-10-05: 0 new, 0 resolved."]

    def test_everything_resolved_says_so(self):
        fake = FakeYouTrack()
        post(fake, A)
        post(fake)
        assert fake.issues[0]["description"].endswith("Nothing is open.")

    def test_an_item_is_one_line(self):
        fake = FakeYouTrack()
        post(fake, "`eso/a`: line one\n  line two")
        assert card.items_of(fake.issues[0]["description"]) == ["`eso/a`: line one line two"]
