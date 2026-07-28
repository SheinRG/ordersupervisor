# Walkthrough video — shot list

The deliverable asks for a video showing eight specific things. This is a
running order that hits all of them in ~6 minutes without dead air.

**Before you record**

- All four services up (see README). One worker only.
- Two browser windows: the app on `:3000`, Temporal's UI on `:8233`.
- Supervisor template `default` — `time_scale=3600`, so 6 business hours = 6s.
- Decide your agent mode. `scripted` is reliable; `grok` is more impressive.
  If using Grok, do a throwaway run first to confirm the key works.
- **Don't leave a run idle for 12+ real minutes mid-recording** — the max-age
  rule will complete it out from under you.

---

### 1. Supervisor config · ~40s
Open **Supervisors**. Walk through one template: base instruction, the five
available actions, wake aggressiveness, the two models, and `time_scale`.

> "The classifier is a cheaper model than the main agent — the spec asks for a
> lightweight gate, so it's literally a lighter model."

Toggle `message_customer` off on the *hands-off* template to show actions are
configurable per supervisor.

### 2. Start a run · ~20s
**Runs → Start supervisor.** Land on the run detail page.

> "One Temporal workflow per order. The workflow id is the order id, so
> starting a second supervisor for this order gets rejected by the server."

Point at the first inference: the agent already ran once on workflow start.

### 3. Show it asleep — in Temporal's own UI · ~40s
Switch to `:8233`, open the workflow, show the event history:

```
WorkflowExecutionStarted → ActivityTaskCompleted → TimerStarted
```

> "That `TimerStarted` is a durable timer. The workflow is evicted from memory
> entirely right now — no thread, no connection. The server will wake it."

**This is the single most valuable shot in the video.** It's third-party
evidence that the sleep is real.

### 4. Routine event — the classifier declines to wake · ~40s
Back in the app, inject **`payment_confirmed`**.

Timeline shows `event: payment_confirmed` then `wake decision: stay asleep`.
Point out the inference counter **does not move**.

> "The workflow woke, classified the event, and went back to sleep for the
> remainder of its interval. The expensive agent never ran."

### 5. Add a run instruction · ~30s
Type: **"If shipment is delayed, escalate immediately."** → Add.

> "This is added *after* the workflow started, and becomes part of its context
> from here on."

### 6. Urgent event — early wake + actions · ~60s
Inject **`shipment_delayed`**.

Show, in order: `wake decision: WAKE`, `agent reasoning`, then
`action: message_logistics_team` and `action: message_customer`, then
`sleeping`. Point at the **escalated** badge and the instruction being honoured
in the reasoning.

Flip to `:8233`: `WorkflowExecutionSignaled` → **`TimerCanceled`** → activities.

> "Timer cancelled — it woke early because the event mattered."

### 7. Pause, resume, interrupt · ~40s
**Pause** → inject `customer_message_received` → show it queued, inference
count static. **Resume** → it drains and the agent runs.
Then **Interrupt** to force an immediate inference.

### 8. Memory and unknown events · ~30s
Show the memory summary panel — the rolling compact summary, not a transcript.
Inject **`warehouse_fire`** (an unknown event) and show it escalates rather
than being silently dropped.

### 9. Completion is workflow-owned · ~50s
Inject **`delivered`**.

> "The agent has been able to set `recommends_completion` this whole time and
> the workflow logged it and ignored it. Completion is owned by lifecycle
> rules, not the model — a terminal event, a manual terminate, or max age."

Show the run flip to completed and the **final output**: summary, important
actions, learnings, recommendations.

Optionally: on a *second* run, hit **Terminate** instead, to show the manual
path still produces a final summary (because terminate is a signal, not a kill).

### 10. Close · ~20s
Back to the runs list — active and completed runs, flags. One line on the
architecture: workflow owns orchestration, activities own all I/O, agent sits
behind an interface with a deterministic implementation for testing.

---

## Recording tips

- Keep the Temporal UI shots — they're the hardest thing to fake and the
  easiest thing to be convinced by.
- If an LLM call stalls, say so and keep going; Temporal retries it and the
  workflow just waits. That's a feature worth narrating, not an outtake.
- Zoom the browser to ~125%. The UI is dense by design.
