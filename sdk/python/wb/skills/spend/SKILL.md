---
name: spend
description: Run Workbench commands that spend money or publish - rent a GPU machine, publish a release - through their quote and the person's approval. Load it before running any command marked as spending, and whenever a job is awaiting approval.
---

# Quotes and approval

A command that spends money or publishes (`$` in `commands`, "spends" in `describe`) never runs straight
through. Its job stops in `awaiting_approval` with one exact quote, and runs only after the person says yes to
that quote. No setting, budget or schedule approves, and neither do you.

## The steps

1. Prepare with commands that spend nothing (below), so the quote is the one the person expects.
2. `run` the spending command. The answer is `awaiting approval` with the quote: what is rented or published,
   the price, the cap, the most it can cost, when the quote expires, and the quote's id.
3. Show the person the quote as it is, every figure. Do not round, summarise away the cap, or call it cheap.
4. On their yes to that quote, `approve(job=..., quote=...)`. The client then asks the person itself, and the
   job runs on their answer. Where the client cannot ask, the tool answers with the command the person runs in
   a terminal: pass it on.
5. Follow the job to its end with `job`, and report what it cost (`cost` in the envelope).

A quote expires after five minutes and is replaced when the price rises. A replaced quote is a new question:
show the new one, and do not approve the old id. If the person says no, or says nothing, the job stays
waiting; `cancel` it when they do not want it.

Never start a second spending job to get past a refusal, split one into smaller ones, or approve from an
earlier yes: an approval covers one quote of one job.

## Renting a machine

| Command | Does |
|---|---|
| `vast.offers` | lists rentable offers for a card; rents nothing |
| `vast.bundle-check`, `vast.bundle-launch` | checks a bundle, or makes one from a launch file; rent nothing |
| `vast.rent` `$` | rents one offer, runs a bundle on it, fetches the results, destroys the machine |
| `vast.instances` | lists the account's rented machines |
| `vast.destroy` | destroys one rented machine, which stops its billing |
| `vast.charges`, `ledger.costs` | what was charged |

A rental is destroyed when its job ends, at its cap, when billed above its quote, and by the next start's
clean-up. After a rental that failed or was cancelled, run `vast.instances` and report any machine still
listed: a machine left running keeps billing.

## Publishing a release

| Command | Does |
|---|---|
| `release.check` | checks files against the publication allow-list, with no network |
| `release.status` | what a channel already holds for a version; read-only |
| `release.publish` `$` | publishes one version's files to one channel after a yes on its plan |

The quote of `release.publish` is its plan: every file with its SHA-256 and size, the notes' digest, and how
it is released. It asks again when anything differs at the time it runs, and never publishes over a version
that exists. Run `release.check` and `release.status` first, and show the plan whole.

## Rehearsing

`selftest.spend` costs nothing and goes through quote, approval and a replaced quote as a rental does. Use it
to show the person how approval looks before the first real one.
