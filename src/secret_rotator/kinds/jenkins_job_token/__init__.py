"""The jenkins-job-token kind (design §6): a new remote-trigger token for the Jenkins job its
args name, in the URL its one key holds. Its plan stages that URL with a new token in it, writes
it to the leaf and its copies, syncs every ExternalSecret that reads them, whatever the leaf's
activate, and activates. Only then does it set the token on the job, so no consumer's Secret
holds a URL the job no longer takes (ruling F3); it verifies by re-reading the job. The job holds
one token, so from the rollout until it is set the consumers' new URL does not trigger it yet."""

from collections.abc import Mapping

from secret_rotator.kinds.jenkins_job_token.steps import Generate, SetToken
from secret_rotator.model import Step
from secret_rotator.plan import PlanContext, PlanError, Target, tool_part

ARGS = ("job",)


def job_of(leaf: Target) -> str:
    """The full name of the job the args of the plan's one key name."""
    return leaf.entries[leaf.keys[0]].args["job"]


def is_job(name: object) -> bool:
    """A job's full name: folders and job by /, none empty or padded."""
    return isinstance(name, str) and all(part and part == part.strip() for part in name.split("/"))


class JenkinsJobToken:
    name = "jenkins-job-token"
    per_key = False

    def args_problems(self, args: Mapping) -> list[str]:
        problems = [
            f"{k}: not one of jenkins-job-token's {', '.join(ARGS)}" for k in args if k not in ARGS
        ]
        if not is_job(args.get("job")):
            problems.append("job: not a Jenkins job's full name")
        return problems

    def ask(self, leaf: Target) -> str:
        return "; ".join(leaf.confirms)

    def description(self, leaf: Target) -> str:
        confirm = " You confirm what only you can do." if leaf.confirms else ""
        return (
            f"The tool puts a new remote-trigger token for Jenkins job {job_of(leaf)} in the URL "
            f"the leaf holds and {tool_part(leaf)}. Once every ExternalSecret that reads the leaf "
            f"has synced, it sets the token on the job and re-reads the job.{confirm}"
        )

    def plan(self, leaf: Target, ctx: PlanContext) -> list[Step]:
        if len(leaf.keys) != 1:
            raise PlanError(
                f"{leaf.leaf}: a jenkins-job-token plan rotates one key, not {len(leaf.keys)}"
            )
        job, key, jenkins = job_of(leaf), leaf.keys[0], ctx.steps.jenkins
        readers = [leaf.leaf, *dict.fromkeys(c.leaf for c in leaf.copies)]
        steps = [
            Generate(jenkins, job, leaf.leaf, key),
            *ctx.steps.write(),
            *ctx.steps.eso_sync_and_rollout([], readers),
        ]
        planned = {step.id for step in steps}
        return [
            *steps,
            *(step for step in ctx.steps.activate() if step.id not in planned),
            SetToken(jenkins, job, leaf.leaf, key),
        ]
