---
name: Feature request
about: A new platform, style, CLI flag, service route or deploy target
title: "[feat] "
labels: enhancement
assignees: smartlotus
---

## The problem in one sentence

What did you want to do and could not?

## Concrete example

A real link or a real workflow is worth more than a generic idea. If it is a
platform request, include:

- platform and example URL:
- does a logged-in browser page show subtitles for it? (yes / no / untested)
- what `pvc resolve --url "<url>"` returns today:

## Proposed shape

- CLI: `pvc <subcommand> --flag` and its place in the JSON envelope:
- HTTP: new route or new field, and whether it needs the bearer token:
- config: new `PVC_*` variable with its default:

## Who benefits, and what it costs

Free-tier impact (image size, ffmpeg, RAM, cold start), money impact (LLM
tokens, ASR minutes), and whether an agent skill file needs the same change.

## Alternatives considered

Including "do nothing, and use the browser relay / the desktop app instead".
