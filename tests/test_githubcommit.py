"""github.commit (design §4.2): one file's new text committed to a repository's main through
GitHub's contents API, with the token a leaf the plan names holds, and read back; a re-run after the
commit landed commits nothing more, a push to the file meanwhile is read again and retried, and a
commit GitHub refused did not land. Its undo commits back the text the commit replaced; without one,
a landed commit refuses Abort."""

import pytest
from fake_github import CONTENTS_TOKEN, FakeGitHub
from fixtures import compliant_store
from plans import LEAF, Journal, RandomLike, Recorder, Tool, client, fake, lock, run_state

from secret_rotator.audit import audit
from secret_rotator.executor import AbortRefused, Executor, Outcome
from secret_rotator.githubsteps import CONFLICT_TRIES, GitHubCommit
from secret_rotator.model import Action, Finished, Progress, value_name
from secret_rotator.plan import build, target
from secret_rotator.staging import staging_leaf

REPO = "pvginkel/Ansible"
PATH = "ansible/roles/bootstrap/files/ansible.pub"
OLD = "ssh-ed25519 AAAAold ansible\n"
THEIRS = "ssh-ed25519 AAAAtheirs ansible\n"
TOKEN_LEAF = "rotator/test/contents"  # key token
MESSAGE = "rotate the Ansible SSH key"
STEP = f"github.commit:{REPO}/{PATH}"
HEAD = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CONTENTS = f"/repos/{REPO}/contents/{PATH}"
WHY = "the test's commit cannot be taken back"


def new_text(ctx, held):
    """The generated token as the file's whole text, whatever the file held."""
    return ctx.staged(value_name("token")) + "\n"


class Committing(RandomLike):
    """random's shape with the commit of the new value between the generate and the write."""

    def __init__(self, *extra, no_undo="", text=new_text):
        super().__init__(*extra)
        self.no_undo = no_undo
        self.text = text

    def plan(self, leaf, ctx):
        commit = ctx.steps.commit_file(
            REPO, PATH, self.text, credentials=TOKEN_LEAF, message=MESSAGE, no_undo=self.no_undo
        )
        return [*ctx.steps.generate("token"), *commit, *ctx.steps.write(), *self.extra]


class World:
    """The plan on the fake OpenBao, which holds the commit's token leaf, with the Ansible repo on
    the fake GitHub; extra: steps after the write."""

    def __init__(self, *extra, no_undo="", files=None, text=new_text):
        self.bao = fake()
        self.bao.leaves[TOKEN_LEAF] = {"data": {"token": CONTENTS_TOKEN}, "meta": {}}
        self.github = FakeGitHub()
        files = {PATH: OLD, "README.md": "readme\n"} if files is None else files
        self.github.repo(REPO, files, sha=HEAD)
        kind = Committing(*extra, no_undo=no_undo, text=text)
        self.plan = build(
            kind,
            target(LEAF, "random", ["token"], audit(compliant_store())),
            github=self.github.github(),
        )
        self.recorder = Recorder()
        self.executor = Executor(
            client(self.bao),
            self.plan,
            self.recorder,
            lock(self.bao),
            state=run_state(self.bao),
            dry_run=False,
        )

    def staged(self):
        """The text the plan in flight commits."""
        return self.bao.data(staging_leaf("random", LEAF))[value_name("token")] + "\n"

    def text(self):
        return self.github.head(REPO)["files"][PATH]

    def commits(self):
        """The commits on main after the first."""
        return self.github.repos[REPO][1:]

    def puts(self):
        return [r for r in self.github.requests if r[0] == "PUT"]

    def failure(self):
        (failed,) = self.recorder.failures()
        return failed

    def details(self, action=Action.RUN):
        """The detail of each finished line of the action, by step id."""
        return {
            e.step.id: e.detail
            for e in self.recorder.events
            if isinstance(e, Finished) and e.ok and e.action is action
        }

    def progress(self, action=Action.RUN):
        return [
            e.detail
            for e in self.recorder.events
            if isinstance(e, Progress) and e.step.id == STEP and e.action is action
        ]


def failing():
    """A step after the write that fails once, so the plan stops past its commit."""
    return Tool("after", Journal(), fail=1)


class TestThePlan:
    def test_the_factory_builds_a_mutating_github_commit_with_an_undo(self):
        (step,) = [s for s in World().plan.steps if s.id == STEP]
        assert isinstance(step, GitHubCommit)
        assert step.title == f"commit {PATH} to {REPO}"
        assert step.mutates and step.undo is not None and not step.activator
        assert step.staged == f"{STEP}:commit"

    def test_no_undo_says_why_it_has_none(self):
        (step,) = [s for s in World(no_undo=WHY).plan.steps if s.id == STEP]
        assert step.undo is None and step.no_undo == WHY


class TestACommit:
    def test_it_commits_the_new_text_to_main_as_the_rotator_and_reads_it_back(self):
        world = World()
        assert world.executor.run() is Outcome.DONE, world.recorder.failures()
        (commit,) = world.commits()
        assert commit["message"] == "rotate the Ansible SSH key (secret-rotator)"
        assert commit["files"] == {
            PATH: world.bao.data(LEAF)["token"] + "\n",
            "README.md": "readme\n",
        }
        assert world.details()[STEP] == f"{commit['sha'][:7]}: {PATH} committed to main"
        assert [(m, p) for m, p, _, _ in world.github.requests] == [
            ("GET", CONTENTS),
            ("PUT", CONTENTS),
            ("GET", CONTENTS),
        ]
        # The token is the plan's leaf's, read when the step runs; rotator/github is never read.
        assert ("GET", f"kv/data/{TOKEN_LEAF}") in [r[:2] for r in world.bao.requests]
        assert not any("rotator/github" in r[1] for r in world.bao.requests)

    def test_a_re_run_after_the_commit_landed_commits_nothing_more(self):
        world = World()
        world.github.refused["PUT", CONTENTS] = 502  # GitHub committed, and its answer was lost
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().error == f"PUT {CONTENTS}: HTTP 502: Refused"
        world.github.push(REPO, PATH, world.staged(), "the commit whose answer was lost")
        del world.github.refused["PUT", CONTENTS]
        assert world.executor.run() is Outcome.DONE, world.recorder.failures()
        assert len(world.commits()) == 1 and len(world.puts()) == 1
        assert world.details()[STEP] == f"{PATH} on main holds the new text already"

    def test_a_push_to_another_file_meanwhile_does_not_conflict(self):
        world = World()
        world.github.racing.append(lambda repo: world.github.push(repo, "README.md", "theirs\n"))
        assert world.executor.run() is Outcome.DONE, world.recorder.failures()
        _, ours = world.commits()
        assert ours["files"]["README.md"] == "theirs\n"
        assert len(world.puts()) == 1

    def test_a_push_to_the_file_meanwhile_is_read_again_and_retried(self):
        world = World()
        world.github.racing.append(lambda repo: world.github.push(repo, PATH, THEIRS))
        assert world.executor.run() is Outcome.DONE, world.recorder.failures()
        theirs, ours = world.commits()
        assert theirs["files"][PATH] == THEIRS
        assert ours["files"][PATH] == world.bao.data(LEAF)["token"] + "\n"
        assert len(world.puts()) == 2
        assert world.progress() == [f"{PATH} changed as it was committed: reading it again"]

    def test_a_file_that_conflicts_at_every_try_did_not_land(self):
        world = World()
        for n in range(CONFLICT_TRIES):
            text = f"ssh-ed25519 AAAA{n} ansible\n"
            world.github.racing.append(lambda repo, text=text: world.github.push(repo, PATH, text))
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"{REPO} {PATH} changed under each of {CONFLICT_TRIES} commits"
        )
        assert all("secret-rotator" not in c["message"] for c in world.commits())
        assert world.executor.abort() is Outcome.CANCELLED
        assert world.text() == f"ssh-ed25519 AAAA{CONFLICT_TRIES - 1} ansible\n"

    def test_a_commit_github_refuses_did_not_land(self):
        world = World()
        world.github.refused["PUT", CONTENTS] = 403
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().error == f"PUT {CONTENTS}: HTTP 403: Refused"
        assert world.commits() == []
        assert world.executor.abort() is Outcome.CANCELLED
        assert world.commits() == []

    @pytest.mark.parametrize(
        ("knob", "answer", "error"),
        [
            ("broken", ConnectionResetError(), f"PUT {CONTENTS}: transport error: "),
            ("refused", 502, f"PUT {CONTENTS}: HTTP 502"),
        ],
    )
    def test_a_commit_whose_answer_is_lost_counts_as_landed_and_its_undo_finds_nothing_to_undo(
        self, knob, answer, error
    ):
        world = World()
        getattr(world.github, knob)["PUT", CONTENTS] = answer
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().error.startswith(error)
        assert world.executor.abort() is Outcome.ROLLED_BACK
        assert world.details(Action.UNDO)[STEP] == f"{PATH} on main holds the text it held before"
        assert world.commits() == [] and world.text() == OLD

    def test_without_its_token_nothing_is_committed(self):
        world = World()
        del world.bao.leaves[TOKEN_LEAF]
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"{TOKEN_LEAF} cannot be read: no such leaf, or its current version is deleted"
        )
        assert world.github.requests == []
        assert world.executor.abort() is Outcome.CANCELLED

    def test_a_file_main_does_not_hold_is_not_created(self):
        world = World(files={"README.md": "readme\n"})
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().error == (
            f"{REPO} has no {PATH} on main, or the token in {TOKEN_LEAF} lacks the repository"
        )
        assert world.puts() == []
        assert world.executor.abort() is Outcome.CANCELLED

    def test_a_file_that_does_not_read_back_as_committed_fails_it_as_landed(self):
        world = World()

        def theirs_after(repo, sha):
            world.github.on_commit.clear()
            world.github.push(repo, PATH, THEIRS)

        world.github.on_commit.append(theirs_after)
        assert world.executor.run() is Outcome.FAILED
        ours, _ = world.commits()
        assert world.failure().error == (
            f"{REPO} {PATH} on main does not hold the text {ours['sha'][:7]} committed"
        )
        assert world.executor.abort() is Outcome.ROLLED_BACK
        assert world.text() == OLD


class TestTheUndo:
    def test_a_rollback_commits_back_the_text_the_commit_replaced(self):
        world = World(failing())
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().step.id == "after"
        new = world.text()
        assert new != OLD
        assert world.executor.abort() is Outcome.ROLLED_BACK
        ours, back = world.commits()
        assert ours["files"][PATH] == new
        assert back["message"] == 'revert "rotate the Ansible SSH key" (secret-rotator)'
        assert world.text() == OLD
        assert world.details(Action.UNDO)[STEP] == f"{back['sha'][:7]}: {PATH} committed back"

    def test_after_a_conflict_it_commits_back_the_text_its_commit_replaced(self):
        world = World(failing())
        world.github.racing.append(lambda repo: world.github.push(repo, PATH, THEIRS))
        assert world.executor.run() is Outcome.FAILED
        assert world.executor.abort() is Outcome.ROLLED_BACK
        assert world.text() == THEIRS

    def test_a_re_run_after_its_commit_landed_keeps_the_text_that_commit_replaced(self):
        world = World(failing())

        def theirs_after(repo, sha):
            world.github.on_commit.clear()
            world.github.push(repo, PATH, THEIRS)

        world.github.on_commit.append(theirs_after)
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().step.id == STEP  # its read-back found their push
        world.recorder.events.clear()
        assert world.executor.run() is Outcome.FAILED  # the commit again, over theirs
        assert world.failure().step.id == "after"
        assert world.executor.abort() is Outcome.ROLLED_BACK
        assert world.text() == OLD

    def test_a_run_that_found_the_file_holding_the_text_has_nothing_to_commit_back(self):
        world = World(failing(), text=lambda ctx, held: held)
        assert world.executor.run() is Outcome.FAILED
        assert world.failure().step.id == "after"
        assert world.details()[STEP] == f"{PATH} on main holds the new text already"
        assert world.executor.abort() is Outcome.ROLLED_BACK
        assert world.details(Action.UNDO)[STEP] == (
            f"nothing to commit back: the step committed nothing to {PATH}"
        )
        assert world.puts() == [] and world.text() == OLD

    def test_a_push_to_the_file_during_the_undo_is_read_again_and_retried(self):
        world = World(failing())
        assert world.executor.run() is Outcome.FAILED
        world.github.racing.append(lambda repo: world.github.push(repo, PATH, THEIRS))
        assert world.executor.abort() is Outcome.ROLLED_BACK
        assert world.text() == OLD
        assert world.progress(Action.UNDO) == [
            f"{PATH} changed as it was committed: reading it again"
        ]


class TestWithoutAnUndo:
    def test_a_landed_commit_refuses_abort_with_why(self):
        world = World(failing(), no_undo=WHY)
        assert world.executor.run() is Outcome.FAILED
        with pytest.raises(AbortRefused, match=WHY):
            world.executor.abort()
        assert world.text() != OLD

    def test_a_commit_github_refused_lets_abort_cancel(self):
        world = World(no_undo=WHY)
        world.github.refused["PUT", CONTENTS] = 422
        assert world.executor.run() is Outcome.FAILED
        assert world.executor.abort() is Outcome.CANCELLED
        assert world.text() == OLD

    def test_it_stages_nothing_to_commit_back(self):
        world = World(failing(), no_undo=WHY)
        assert world.executor.run() is Outcome.FAILED
        staged = world.bao.data(staging_leaf("random", LEAF))
        assert f"{STEP}:commit" in staged and f"{STEP}:before" not in staged
