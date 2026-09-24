---
name: Bug
about: A defect, with the evidence that shows it and the behaviour that would replace it
title: '[bug] '
labels: type:bug
---

## Objective
The wrong behaviour, and the right one. Quote the evidence: a command, its output, a file:line.

## Acceptance criteria
- The failing case, written as a statement that will hold once it is fixed.
- A test that reproduces it.

## Stages
- [ ] One small unit of work that leaves tests green and is committed; name the files it touches and how it is verified.

## Context
- Module docs, ADRs and paths to read. Only the ones that are actually needed.

## Not included
- Neighbouring defects this one does not cover, with the issue that owns them.

## Dependencies
none

## Definition of done
Tests, docs and the commit/PR this lands as.

<!-- budget: mechanical-qwen -->
