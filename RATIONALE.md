# faultline — design rationale

*working draft for the written submission and approximately five-minute video. time spent so far: **approximately 3.5 hours**, starting around **5 pm**. implementation and verification are ongoing; this is not the final time total.*

## why this problem

i chose **theme 3: systems & reliability** because a failed tool response can leave an agent with a difficult decision: did nothing happen, or did the operation succeed and only the acknowledgment disappear? retrying a write without checking can make the result worse.

faultline makes that decision observable. a small agent harness works on a bundled python repository inside an isolated modal sandbox. a separate environment introduces missing files, denied writes and lost acknowledgments. the browser shows the commands, outputs, file changes and recovery checks. the goal is a developer tool for reproducing and inspecting agent failures, with an evaluation component to distinguish finishing a task from recovering carefully.

## the central experiment

the release scenario asks the agent to bump a version and add a changelog entry. the environment performs the write, then returns a timeout-style tool error. a careful agent reads the file before deciding whether another write is needed; a careless append retry creates a duplicate.

verification checks both behavior and outcome: a read before another write, exactly one release entry, the correct version and passing tests. saved scripted controls scored 100 for careful recovery and 8 for a careless duplicate append. these are results for this fixture and rubric, not a general benchmark of model reliability. recorded live agent runs are bundled with the browser app so a reviewer can inspect the experiment without model quota or local setup.

## architecture and tradeoffs

- **separate harness and environment.** `apps/web`, `services/agent-harness` and `services/sandbox-env` deploy independently on modal. the environment exposes tools over mcp so another harness can use the same tasks and fault machinery. service hops add overhead but separate model decisions from execution and verification.
- **real execution, explicit simulation.** commands operate on real files. transient missing-file and denied-write faults are simulated at the tool boundary; the sticky missing-file case starts with a file absent. lost-ack performs the operation and substitutes an error response, without breaking the connection. shell fault matching is limited and token-based.
- **persistent history.** the harness runs separately from the browser's event stream. a single-writer sqlite store holds runs, events and conversations, with snapshots on a modal volume. saved verification demonstrates history surviving redeployment. periodic snapshots leave a potential loss window; persisted history alone does not establish worker recovery.
- **bounded execution and credentials.** each episode gets an isolated sandbox with network access blocked and a limited lifetime. only the model-calling harness function receives the anthropic key.
- **inspectable grading.** file checks and the execution ledger supplement tests. hidden tests are introduced at evaluation time, but currently execute in the modified workspace. this is not yet a tamper-resistant verifier. the 60/40 task-and-recovery score is a prototype rubric; individual checks matter more than the aggregate.

## what changed during development

a concurrent cleanup terminated a sandbox during a live run. that exposed a distinction the initial design handled poorly: an injected tool failure, lost infrastructure and unavailable grading must not collapse into the same status. it led to explicit failure provenance and a clearer browser explanation of what happened. aligning those semantics across backend events, persisted history and the ui remains part of the finishing work.

## next step and scope

the next experiment is actual harness recovery: complete a shell write, interrupt the harness before acknowledgment, and have a fresh worker resume the same run and workspace without duplicating the write. the scenario and event contract exist, but that recovery path is not yet implemented or verified. it needs acknowledged recovery state and a deterministic interruption point, not an assumed delay.

with more time, i would isolate verification further, measure behavior under concurrent runs, and explore safe ownership transfer when agents share a filesystem. for this take-home, the priority is one defensible end-to-end demonstration within the eight-hour limit.

## use of ai

i used claude code for implementation and parallel work. my role has been choosing the problem, defining the harness/environment boundary, directing fault semantics and grading, and challenging claims against execution evidence. development transcripts still need to be exported for the submission; the application's runtime traces are separate artifacts.
