"""The breadth capabilities `codex-jobs capabilities` lists, each with the tests that pin it.

An entry names the fake-backend tests that pin its behavior (`tests/<file>::<test>`); a test in
`tests/test_cli.py` fails when a named test does not exist, so the suite passing is the gate. The
`delegation` entry is Codex's own behavior, not code here, so it names no test.
"""

CAPABILITIES: tuple[dict[str, object], ...] = (
    {
        "name": "native-review",
        "summary": "review --native --target uncommitted|base:<branch>|commit:<sha>: Codex's built-in reviewer via review/start; review.md only",
        "tests": (
            "test_breadth_native_review.py::test_native_review_leaves_the_message_as_review_md_and_no_findings",
            "test_breadth_native_review.py::test_native_review_sends_review_start_with_the_wire_target_and_no_turn_start",
            "test_breadth_native_review.py::test_a_native_review_with_no_server_is_recovered_from_its_sub_thread_rollout",
        ),
    },
    {
        "name": "images",
        "summary": "--image <file> attaches a local image to the job's turn (consult, review, run, resume, fork)",
        "tests": (
            "test_recovery.py::test_requests_carry_model_sandbox_schema_prompt_and_images",
            "test_mcp_server.py::test_prompt_file_idempotency_key_and_images_reach_the_command",
        ),
    },
    {
        "name": "steer",
        "summary": "steer <id> <text> adds input to the running turn",
        "tests": ("test_cli.py::test_steer_sends_turn_steer_with_the_expected_turn_id",),
    },
    {
        "name": "interrupt",
        "summary": "interrupt <id> ends the running turn; the job settles interrupted",
        "tests": ("test_cli.py::test_interrupt_journals_and_ends_the_turn",),
    },
    {
        "name": "resume",
        "summary": "resume <job-or-thread-id> continues the thread with the source job's cwd, model and effort",
        "tests": (
            "test_breadth_fork_resume.py::test_resume_sends_the_source_jobs_cwd_and_model_and_an_explicit_model_wins",
        ),
    },
    {
        "name": "resume-run",
        "summary": "resume of a finished run job continues it with write access in its worktree, taking over the lease",
        "tests": (
            "test_breadth_fork_resume.py::test_resume_of_a_finished_run_job_continues_in_its_worktree_and_takes_over_the_lease",
            "test_breadth_fork_resume.py::test_resume_of_a_run_job_is_refused_while_it_is_still_running",
        ),
    },
    {
        "name": "fork",
        "summary": "fork <job-or-thread-id> branches the thread into a new read-only job",
        "tests": (
            "test_breadth_fork_resume.py::test_fork_of_a_run_job_without_a_worktree_is_read_only_in_the_parents_directory",
        ),
    },
    {
        "name": "fork-run",
        "summary": "fork of a run job with --branch or --worktree keeps its write access in a worktree of its own",
        "tests": (
            "test_breadth_fork_resume.py::test_fork_of_a_run_job_with_its_own_branch_writes_only_in_its_own_worktree",
        ),
    },
    {
        "name": "delegation",
        "summary": "Codex's own subagents inherit the job's sandbox and run inside its app-server: this machine's cap counts them as one job",
        "tests": (),
    },
)
