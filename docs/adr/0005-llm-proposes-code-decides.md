# ADR 0005: The LLM proposes, code decides, a human approves money

**Status:** accepted (P3 TicketPilot)

## Context

A support agent that can issue refunds is only useful if it can act without a human on the
common, cheap cases, and only safe if it can never act wrongly on the expensive ones. LLMs are
good at reading a messy ticket and finding the right order. They are bad at being a security
boundary: anything in the context window (the ticket, a tracking note) can steer them.

## Decision

1. **The model gets read tools and *propose* tools only.** `propose_refund`,
   `propose_address_change`, `no_action_needed` and `escalate` end the model's loop; none of them
   has a side effect.
2. **A pure-function policy engine decides** (`domain/policy.py`): auto / approval / deny, from
   order data re-fetched by code, never from the model's claims. Thresholds live in
   `config/policy.yaml`.
3. **Writes happen in exactly one node** (`execute`), with an idempotency key derived from the
   run, so a retry or resume can never double-pay.
4. **Approvals are a LangGraph `interrupt()`** persisted by the Postgres checkpointer. The
   supervisor's decision resumes the same thread, even after a process restart.
5. **A rules baseline uses the same graph** with the agent node swapped out, so the eval
   measures what the LLM adds, not what the guardrails add.

## Consequences

- The unsafe-action rate is bounded by the policy, not the prompt: a fully compromised model can
  at worst cause an escalation or a refund the policy would have allowed anyway.
- Policy changes are code reviews with a decision-table test, not prompt tweaks.
- Cost: an extra deterministic re-fetch per proposal (milliseconds), and some tickets a smarter
  system could resolve go to humans instead.
- The residual risk is forged-but-valid intent (see the threat model).

## Alternatives rejected

- **Give the model write tools with a "be careful" prompt.** Untestable; one injection away from
  an incident.
- **Ask the model to self-check before writing.** Same model, same context, same blind spots.
- **Human approval for every action.** Safe but removes most of the value; the USD 50 line keeps
  ~half the refunds automatic in the scenario set.
