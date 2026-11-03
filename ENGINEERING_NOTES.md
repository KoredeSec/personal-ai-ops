# Engineering Notes

This is the running log for Personal AI Ops — a build journal, not a spec. The
README tells you how to run the thing. This file tells you *why* it is the way
it is: the decisions, the trade-offs, the things I argued myself out of, and the
mistakes I only caught because a test failed.

I'm keeping it because I'm building this in the open and I'd rather show the
reasoning than a polished result that hides it. If you're reading this to build
your own version, the dead ends are probably more useful than the conclusions.

Newest entries are at the top. Dates are Africa/Lagos.

---

## The bet I'm making

Between work, personal projects and research, the thing I'm short on isn't
information — it's *decisions*. A to-do list doesn't help if deciding what to do
next is itself the tiring part. So the goal of this system isn't to do more. It's
to take decisions off my plate: what to work on now, what's slipping, what's
worth reading, what to ignore.

That framing drives a rule I keep coming back to: **only call the model when
language is genuinely the hard part.** Listing overdue tasks, converting a
timezone, checking a deadline — that's arithmetic, and arithmetic should be
boring, fast, free and testable. I spend the model (and the Gemini free-tier
quota) on the parts that are actually fuzzy: reading a sentence I typed in a
hurry, summarising an article, judging whether an opportunity fits. Everything
else is plain Python.

Two agents, one bot:
- **Chief of Staff** — tasks, reminders, follow-ups, briefings, memory.
- **Research Agent** — monitors security sources and scholarship/career
  opportunities, and summarises what matters.

---

## On two comments I got early

I posted about this before it was finished and got two questions that are
sharper than they look. Writing the answers down because they're shaping the
design.

### "Track how many decisions the agents remove, not just how many tasks they finish."

This is the whole point, stated better than I stated it. And the warning tucked
inside it is the real risk: **a Research Agent that produces great reading lists
is quietly handing the Chief of Staff more things to prioritise.** It feels
productive while making my day heavier. Two agents can look busy and still leave
me with more open loops than I started with.

So I don't want a metric that rewards volume — articles found, tasks created.
Those go *up* when the system is doing badly. What I actually want to watch:

- **Decisions deferred to me vs. handled without me.** A reminder that fired on
  time removed a decision. An opportunity dumped on me with no verdict *added*
  one. I already log every action to `agent_execution_logs`; the plan is to tag
  each one as "resolved" or "handed back to the owner" so the ratio is
  measurable rather than a vibe.
- **Research that dies in the inbox.** If the Research Agent surfaces things I
  never act on, that's not a neutral outcome — it's noise with a cost. I'd
  rather it show fewer, higher-confidence items and measure how many I actually
  open or act on, not how many it found.
- **Net open loops.** Tasks and follow-ups created minus closed, over a week. If
  the system is working, this trends down or holds flat while I feel calmer. If
  it climbs, the agents are generating work, not absorbing it.

None of this is built yet. It's a Milestone 2–3 concern. But I'm writing the
execution log now (Milestone 1) with these questions in mind, so the data exists
when I'm ready to ask.

### "Does the Chief of Staff choose what to research, or do you assign it?"

Right now: **I assign it.** And I want to be deliberate about keeping it mostly
that way, at least to start.

The tempting design is a hierarchy — Chief of Staff notices a gap ("you keep
reading about detection engineering") and dispatches the Research Agent on its
own. That's a fun demo and a bad idea this early, for two reasons. First, it
spends quota on work I didn't ask for, which is exactly the "busy agents, heavier
day" trap from the first comment. Second, autonomous agent-to-agent dispatch is
how you get a system that confidently does the wrong thing at scale while you're
asleep.

So the relationship I'm building toward, in order of how much I trust it:

1. **I assign research directly.** `/research <topic>`, a URL to analyse, or a
   standing source list. This is Milestone 3 and it's the default.
2. **Scheduled research I opted into.** Daily security briefing, weekly digest,
   scholarship-deadline watch. I set these up once; they're not the agent
   deciding, they're me deciding on a recurring basis.
3. **The Chief of Staff may *suggest* research, never launch it.** If it notices
   a pattern, the most it does is add a line to a briefing: "You've got three
   tasks tied to cloud security — want me to track AWS/Azure advisories?" I say
   yes or no. The suggestion is cheap; the action waits for me.

The through-line is the same promise as the "never send messages without
approval" rule: **the agents can prepare, surface and recommend; crossing into
action — spending quota, contacting people, committing my time — waits for a
human yes.** If I ever relax that, it'll be one narrow path at a time, written
down here first.

---

## 2026-11-03 — Getting ready to build in public

Before the repo goes public, a few things that are easy now and painful later.

**Commit email.** My early commits used my personal email, which a public repo
would expose to anyone who clones it. I'm rewriting the history to GitHub's
no-reply address while it's three commits long and has never been pushed.
Rewriting history after people have cloned it is the painful version of this.

**CI before visitors.** People judge a public project by whether the checks are
green, so CI goes in before the repo is public, not after. On every push it runs
the offline test suite, applies the migrations to a fresh database and confirms
the models match them, audits the pinned dependencies for known CVEs, scans the
*entire* git history for secrets, and builds the Docker image and confirms it
doesn't run as root.

The secrets scan had a small irony to solve. My tests are full of fake bot tokens
and API keys, because the test that proves real ones get redacted needs
something that looks real. A scanner would flag every one of them. The fix is an
allowlist, and I kept it deliberately narrow: it matches those exact fake
patterns inside test files only, not "ignore the tests folder". If a real key
ever ends up in a test by accident, it still gets caught.

I also wanted the scanner pinned to an exact version and run from its official
image, rather than whatever a third-party action happens to fetch that day.
Supply chain matters more for a tool whose whole job is to touch my credentials.

**MIT licence**, so anyone can take this and adapt it, which was the point of
sharing it. And a `SECURITY.md` that says to report problems privately rather
than in a public issue, with a scope list that's really just a summary of the
promises this system makes: the allowlist holds, secrets don't leak, fetched
content can't steer the agents, and nothing external happens without my yes.

**Where off-machine backups should go: still open.** Nightly backups exist, but
they sit on the same disk as the data, so one dead disk takes both. My current
leaning is to encrypt each backup on the machine and push it to a separate
private GitHub repo, using a key that can only write to that one repo. GitHub
would only ever hold ciphertext, and the decryption key would never live on the
server. It costs nothing and needs no new account. I'm deciding properly once
Milestone 2 is storing data that's actually worth protecting.

---

## 2026-11-03 — Milestone 1.1: making it trustworthy before making it bigger

I finished the Milestone 1 foundation, then ran a design critique over it before
adding features. The critique was blunt and right: the foundation was *secure*
but not yet *trustworthy*. Those are different. Secure means strangers can't get
in. Trustworthy means that when I come to rely on this daily, it doesn't quietly
drop a reminder or freeze for two minutes and leave me wondering if it's dead.

I stopped and fixed reliability before touching Milestone 2. A tool I don't
trust by reflex is one I'll stop opening, and then none of the features matter.

**One slow AI call could freeze everything.** I'd set the bot to process one
message at a time, which is simple and tidy — until an AI call sits there
retrying for a minute and every other command waits behind it. So `/tasks` could
hang because `/aitest` was slow. Fixed two ways: the bot now handles several
messages at once, and any AI call I'm actively waiting on has a hard 20-second
budget including retries, after which it gives up with an honest message. The
long, patient retry policy is still there — but only for background jobs, where
nobody's watching a cursor blink. There's a test that fires a deliberately slow
AI call and then checks a `/ping` still comes back immediately.

**Reminders could be lost on a crash.** This is the one that would actually erode
trust, because a reminder system that *sometimes* forgets is worse than none —
you stop relying on it but keep half-expecting it. The naive version marks a
reminder "sent" and fires it; crash in the gap and it's either lost or sent
twice. I reworked it into claim → send → confirm, with every send recorded under
a stable key like `reminder:42`. Overlapping runs can't double-send. A send
that died mid-flight gets reclaimed after a 5-minute lease instead of vanishing.
The one remaining failure mode is a crash in the millisecond between Telegram
accepting the message and my database recording it, which can cause a *duplicate*
— and I chose that on purpose. For a personal reminder, a rare "you'll see this
twice" beats a rare "you never saw it." There are tests for each crash point,
including two dispatchers racing each other.

**A hung bot still reported healthy.** The old health check only asked "can I
open the database," which a completely frozen bot passes. And I learned while
fixing it that Docker won't restart a container just for being *unhealthy* —
unhealthy is a label, not an action. So I added a heartbeat the bot writes every
minute and a watchdog on a separate thread that kills the process if the
heartbeat goes stale, letting Docker's restart policy actually bring it back. The
separate thread matters: if the main loop is wedged, a watchdog living on that
same loop is wedged too.

**No backups until data existed.** Backups were slated for Milestone 4, but real
personal data starts arriving in Milestone 2, so that ordering was backwards. I
moved them forward: a nightly SQLite backup using the online-backup API (safe
while running), each one integrity-checked before it's kept, oldest pruned, with
a restore command that saves the current database aside first so a restore is
itself reversible. Known gap, written down honestly: the backups sit on the same
disk as the data. Off-machine copies are on the later list.

**Failures were silent.** A tool that fails quietly trains you to distrust it
everywhere. So: a failed background job now pings me once a day, `/status` leads
with a plain "All systems OK" or a numbered list of what's wrong, and the
rate-limit message tells me exactly when it lifts instead of just going quiet.

I also collapsed every reply through one path that escapes text and splits long
messages, so I can't accidentally ship a formatting bug or an over-long message
later. And I pulled the word "Milestone" out of everything the user sees — that's
my scaffolding, not theirs.

113 tests, green, and green across five back-to-back runs because several of
these tests involve timing and I didn't trust a single pass.

---

## 2026-11-03 — Milestone 1: the foundation

The goal here was deliberately unglamorous: get the skeleton right so later
milestones add behaviour, not foundations. The things I decided early, and why:

**No agent framework.** I kept getting pulled toward the big orchestration
libraries and kept deciding against them. For a two-agent personal system they
add concepts I'd have to learn and debug without removing work I actually have.
Agents are plain Python modules. If that stops scaling, I'll reconsider — but I'd
rather hit the wall than cargo-cult around one that isn't there.

**UTC everywhere inside, Lagos everywhere outside.** Timezones are where these
systems rot quietly. I store every instant in UTC and only convert to Lagos time
at the moment of display. To stop myself cheating, the database column type
*rejects* a naive datetime outright rather than guessing — so a mistake fails
loudly in a test instead of silently firing a reminder an hour off.

**Migrations from day one, never auto-create tables.** Auto-creating tables is
convenient right up until the schema changes under real data and there's no path
forward. Everything goes through Alembic, there's a test that fails if the models
and migrations ever drift apart, and the app refuses to start on an out-of-date
schema rather than limping along. SQLite now; the whole thing is written to move
to PostgreSQL by changing one setting.

**One place that talks to Gemini.** Every model call goes through a single client
that owns timeouts, retries with backoff, a local daily request budget to protect
the free tier, a prompt-size cap, optional caching of repeatable work, structured
JSON validation, and — importantly — a *distinct, honest message* for each way it
can fail. "Quota's gone for today" and "the key's wrong" and "service is down"
are different problems with different fixes, and lumping them into "something went
wrong" would make this impossible to debug later. Credentials come only from the
environment and are scrubbed from every log line, including inside tracebacks and
the request URLs the HTTP library likes to print — a test that checks for exactly
that leak caught a real one.

**Deny by default, and stay invisible to strangers.** Only my Telegram ID is
served, only in private chats, and an unknown user gets *silence* rather than
"you're not authorised" — no point confirming the bot exists to someone probing
it. Every denial is audited (throttled, so a flood can't balloon the log).

**Treat all external content as hostile data.** This is a down-payment on the
Research Agent. Anything fetched from the web or a feed will be wrapped so the
model treats it strictly as data to analyse, never as instructions to follow,
and there's a URL validator that refuses anything pointing at internal or private
networks. The agent isn't built yet, but the day it starts reading the open web,
the guardrails are already there and already tested rather than bolted on after.

The honest status at the end of Milestone 1: everything above is tested offline,
including full bot conversations run through the real Telegram library over a
fake connection. What I *cannot* verify from my build environment is a real
round-trip to Telegram or a real Gemini answer — the environment blocks the
outbound calls. So those remain explicitly unverified until I run it on my own
machine, and I'd rather say that than claim it works.

---

## Running list of things I'm deferring on purpose

Not forgotten — parked, with the reason.

- **Off-machine backups.** Encrypt and copy the nightly backup somewhere else.
  Today a disk failure loses both the data and its backups. Low effort, real gap.
- **A "decisions removed" metric.** Needs the execution log tagged by outcome
  (see the comments section). Waiting until there's real usage to measure.
- **Chief of Staff suggesting research.** The suggestion-not-action path from
  above. Wants both agents to exist first.
- **Calendar read access.** Conflict detection and "free time for study" would be
  far better against a real calendar than against tasks alone. Needs a one-time
  Google sign-in; not committing to it until the core loop earns its keep.

---
