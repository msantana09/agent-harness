# Reviewing and applying a proposal

## Changes

A proposal holds every change its fix needs, often more than one: a rule, the memory behind it,
and a hook that enforces it. Each change is one of:

- **edit**: a path, the exact before text, and the after text.
- **new file**: a path that does not exist yet, and the whole file.
- **delete**: a path that exists.

List them in the order they are applied, each change before the ones that depend on it: a hook
script before the settings entry that runs it.

## Review

Before asking for the yes, give a fresh subagent the proposed changes, the quoted evidence and the
paths of the target files. It reads each target file in full and reports any change that does not
match the diagnosis, reads badly in its place, or contradicts the rest of the file.

Fix each finding, or drop the change it names together with the changes that depend on it. Never
apply a proposal with an open finding. Put the findings and how each was resolved in the report.

## Apply the scored text

The text applied is the text the eval scored. A fix from the review may reword it but adds no
steps, cases or examples. Anything more means running the eval again on the new text; keep it only
if it scores higher than the scored version, and otherwise apply the scored version.

## Apply

On the user's yes, take the changes in the listed order. Before each one, check the file:

- an edit's before text still appears exactly once;
- a new file's path does not exist;
- a deleted file exists.

When a check fails, or the file around the before text has changed since the diagnosis read it,
skip that change and the changes that depend on it, and report each skip. Never re-derive the edit
or adapt it to the file as it is now. Make the change the way the user's own instructions say
changes to that file are made. Make no change outside the listed ones.

## Check

After the last change:

- Each after text and new file is in place, and each replaced before text and deleted file is gone.
- The tests that cover each changed script or hook pass. When one fails, show the user the output
  before writing the ledger entry, and let them decide whether to keep or undo the change.
