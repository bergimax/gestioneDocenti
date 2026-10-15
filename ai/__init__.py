"""AI assistance: plain functions, no models, no views. Every Anthropic call
goes through a function here so tests can mock it. Output is a proposal that
the caller stores as a DRAFT; nothing here is authoritative."""
