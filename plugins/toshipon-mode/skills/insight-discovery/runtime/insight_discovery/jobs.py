"""Scheduled work. The foundation's scheduler and queue do the timing, dedup, retry and leases.

Insight Discovery adds two job kinds and an entry point that runs the foundation's tick with
both sets of handlers. It installs nothing: starting the tick on a timer is the user's step.
"""

from . import decisions, pipeline
from . import state as st
from .foundation import Queue, Refusal, dispatch

KINDS = ("insight_daily", "insight_weekly")


def daily(context, job):
    """Differential analysis. A policy refusal is a setting to change, not a failure to retry."""
    home, config = context["home"], context["config"]
    try:
        return pipeline.run_daily(
            home, config, st.load_config(home), context["now"], budget=context["budget"]
        )
    except Refusal as refusal:
        return {"action": "refused", "reason": str(refusal)}


def weekly(context, job):
    """The weekly report. Choosing an experiment is left to a person."""
    return decisions.weekly(context["home"], st.load_config(context["home"]), context["now"])


HANDLERS = {"insight_daily": daily, "insight_weekly": weekly}


def all_handlers():
    return {**dispatch.HANDLERS, **HANDLERS}


def tick(home, config, now):
    """Queue what is due (one slot per entry, however long the machine slept), then work the queue."""
    queued = dispatch.schedule_tick(home, config, Queue(home, config["queue"]), now)
    return {"queued": queued, "jobs": dispatch.dispatch(home, config, handlers=all_handlers())}


def rehearse(home, config, now):
    """Dry run of a tick: which jobs it would queue and what a daily run would do. Writes nothing."""
    known, local = Queue(home, config["queue"]).jobs(), now.astimezone()
    due = [
        f"{entry['kind']}:{dispatch.slot_for(entry, local):%Y-%m-%dT%H:%M}"
        for entry in config["schedule"]
    ]
    return {
        "dry_run": True,
        "would_queue": [job_id for job_id in due if job_id not in known],
        "daily": pipeline.plan(home, config, st.load_config(home), now),
    }
