{{#if read}}
Read a UTF-8 text file inside the repository worktree. `path` is relative to the worktree root. Output is line-numbered.
Absolute paths, `..` escapes, URLs, internal schemes, device paths, `.git` metadata and symlinks leaving the worktree are refused; a refusal ends the whole run.
{{/if}}
{{#if write}}
Write a UTF-8 text file inside the repository worktree, replacing it atomically if it exists or creating it with missing parent directories. `path` is relative to the worktree root and must match an allowed pattern:
{{#each allowedPaths}}
- `{{this}}`
{{/each}}
Patterns match whole path segments only (`*`, `?`, `[..]` within a segment, `**` for any number of segments); a directory name never authorizes the files below it.
Absolute paths, `..` escapes, URLs, internal schemes, device paths, `.git` metadata, symlinked parents or targets, hardlinked targets and paths outside the allowed patterns are refused; a refusal ends the whole run.
{{/if}}
