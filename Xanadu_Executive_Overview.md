# Xanadu — Executive Overview

**What it is:** Xanadu is an autonomous software engineering system. You describe what you want built, it plans the work, writes the code, tests it, and fixes its own mistakes — with a human checking in only at the start and the end, not every step in between. Think of it less like a chatbot you prompt one task at a time, and more like a standing engineering crew you hand a backlog to, then walk away from.

It's not a prototype. It's currently building **Galaxican**, a 30-level real-time strategy game shipping to iOS and Android — live proof the pipeline produces working, tested, shippable code, not just demos.

## The system, in one picture

**See `Xanadu_Diagram.png`** (saved alongside this file) — insert it as an image in Google Docs (Insert → Image → Upload from computer) for the visual. It shows the same flow described below.

Plain-text version of the flow, for reference:

New idea → task backlog
&nbsp;&nbsp;&nbsp;&nbsp;↓
**AUTOMATION** (local-first, escalates only on repeat failure): Tier 1 (fast, cheap, no per-token cost) → Tier 2 (still local) → Tier 3 (still local) → Tier 4 (still local, heavyweight) → Claude (opt-in, metered cost, hard tail only)
&nbsp;&nbsp;&nbsp;&nbsp;↓ successes become working, tested features · failures feed the learning store
**LEARNING**: shared error patterns + coding rules, synced across every machine, feeding back into every tier so it makes fewer repeat mistakes over time
&nbsp;&nbsp;&nbsp;&nbsp;↕ powers and is powered by
**SCALING**: Solo Mac (ad-hoc, walk away, check back later) → Parallel workers (one machine, hardware-capped) → Cloud burst (rented GPUs on demand, 10-50× local capacity)

## Automation, using the right LLM

Every task runs through a ladder of models, cheapest and fastest first — and all four local tiers run on hardware already owned, not billed by the token. A model only escalates to the next, more capable tier after it fails the *same* way twice in a row — one bad attempt doesn't burn the good hardware. Only the hardest fraction of a percent of tasks ever reach a metered, cloud-hosted frontier model, and even that tier is opt-in, not a standing cost.

This is the savings case, stated plainly: the default way to get an AI to write code today is to run every request, easy or hard, through a paid, per-token cloud model — cost scales linearly with how much engineering the team does. Xanadu inverts that. The overwhelming majority of tasks — the routine, well-understood ones that make up most of a backlog — are solved entirely on local hardware for near-zero marginal cost. Token spend only shows up on the genuinely hard tail, which is a small fraction of total volume. Same output, a fraction of the bill.

## Learning — it gets better at your codebase, not just at coding

Every failure is logged, classified, and — once a fix pattern shows up more than once — automatically promoted into a permanent rule the system checks before every future attempt. That rule store is shared across every machine running the pipeline, so a lesson learned on one laptop benefits every worker, everywhere, immediately. The system also doesn't take a new model's word for being better: before promoting one into the default lineup, it races the candidate against the incumbent on real, live tasks and measures actual outcomes — the kind of empirical rigor you'd want from a data team, applied to picking its own tools.

## Scaling — one engine, three gears, and the third one is the competitive edge

The same execution engine runs at three different scales with zero rework: solo on a single Mac for ad-hoc, spare-time work ("have an idea, queue it up, check back later"); multiple parallel workers on one machine when there's a bigger local backlog; and rented cloud GPUs, spun up on demand and torn down when done, for large pushes.

The first two gears are bounded by one machine's hardware — a Mac can only run so many workers in parallel before it runs out of memory and GPU. The third gear removes that ceiling entirely. Renting GPU capacity lets dozens of workers run the exact same pipeline simultaneously, turning a backlog that would take weeks on a single machine into a single overnight run — 500+ tasks in one burst for Galaxican, for example. That's not just a cost lever, it's a speed lever: the ability to compress that much engineering time into that short a window, on demand, whenever a deadline or opportunity calls for it, is a real competitive advantage over teams capped at whatever compute sits on their desk. And because it's rented, not owned, the cost only exists on the days it's used — idle time is free.

## The bottom line

Two numbers matter most to the business. First, cost: because the large majority of work is solved locally instead of through metered cloud tokens, engineering throughput stops scaling linearly with API spend — a direct answer to an industry where token bills climb with every line of AI-assisted code. Second, speed: renting parallel GPU capacity on demand lets the team burst far past what any local machine could do, compressing weeks of work into a single push whenever it matters — a scaling advantage competitors bottlenecked by local-only compute simply can't match. The system also gets measurably cheaper and more accurate the longer it runs, since every fix it learns is shared across every machine, forever.

For engineering: it's a genuinely disciplined pipeline — graduated model escalation, dependency-aware parallel scheduling, a closed empirical learning loop, and elastic compute — proven against a real shipping product, not a slide deck.
