"""Source adapters — what the logs mean (SPEC.md §6.2).

`anthropic` is the first and is written as a plain module. The `SourceAdapter`
protocol is extracted once a second adapter exists: the abstraction that fits
Anthropic alone will be wrong for Bedrock (AGENTS.md).
"""
