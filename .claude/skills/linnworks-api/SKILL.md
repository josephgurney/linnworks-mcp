# Linnworks Architect

Use this skill when a task involves Linnworks API integrations, Linnworks-backed automations, order/inventory/shipping workflows, middleware design, endpoint selection, authentication/session handling, or debugging Linnworks request failures.

## What this skill is for

This repo works with Linnworks APIs and related commerce systems.

Apply this skill when the task involves:
- choosing Linnworks endpoints or endpoint families
- designing or changing Linnworks integration workflows
- Linnworks authentication or session handling
- inventory, stock, locations, listings, orders, shipping, or postal service flows
- debugging Linnworks API failures
- mapping business requirements to Linnworks operations
- building middleware, internal tools, sync jobs, dashboards, or automations using Linnworks data

## Core rules

### 1) Never invent Linnworks details
Do not invent:
- endpoint names
- request bodies
- response shapes
- parameter names
- auth requirements
- status values
- rate-limit behavior
- webhook/event behavior

If the repo or provided docs do not confirm a Linnworks detail:
- state the uncertainty clearly
- implement the safest abstraction
- isolate the uncertain logic behind a clearly named method
- leave a concise verification note if needed

### 2) Start with the workflow, not the endpoint
Before coding, identify:
- the business goal
- the source of truth
- whether this is read-only or a write operation
- whether it is synchronous request/response, scheduled sync, or event-driven automation
- the smallest reliable workflow that satisfies the goal

Then choose the endpoint family that best fits that workflow.

### 3) Authentication must be handled explicitly
For any Linnworks operation:
- identify how authentication is obtained
- identify token/session lifetime assumptions
- centralize auth/session handling
- avoid duplicating auth code across modules
- fail clearly on auth problems

Preferred layering:
- `AuthProvider` or `LinnworksAuthService`
- `LinnworksClient`
- domain services such as `InventoryService`, `OrderService`, `ShippingService`, `ListingService`

Business logic should not construct raw auth/session requests ad hoc.

### 4) Prefer internal abstractions over scattered HTTP
Do not spread raw Linnworks HTTP calls throughout the codebase.

Prefer:
- one shared client wrapper
- typed request builders or validators
- typed or validated response mapping
- domain methods with business meaning

Prefer this:
- `inventoryService.adjustStockLevel(sku, locationId, quantity)`

Over this:
- inline `fetch()`/`axios()` calls from route handlers, jobs, or UI files

### 5) Write operations are operationally sensitive
Any create/update/delete action against Linnworks may affect live operations.

For write paths:
- call out the operational impact
- validate input before sending
- prefer idempotent behavior when possible
- read current state first when practical
- only send the minimum required change
- add structured logs around intent and result
- avoid bulk writes unless explicitly requested
- support dry-run mode when practical

### 6) Read before write when feasible
When changing Linnworks state, prefer:
1. fetch current state
2. compute diff
3. send minimal update
4. verify result if appropriate

This reduces accidental overwrites and makes failures easier to diagnose.

### 7) Observability is part of the implementation
For Linnworks integration paths, add:
- structured logs
- request correlation IDs where the repo supports them
- timeout handling
- retry boundaries
- redaction for tokens and PII
- actionable error messages

Never log:
- tokens
- secrets
- raw credentials
- unnecessary customer-sensitive data

## How to approach Linnworks tasks

### Endpoint selection
When asked which endpoint or API area to use:
- choose the most specific workflow-aligned option
- explain why it fits better than alternatives
- note key request/response concerns
- call out edge cases relevant to orders, stock, shipping, listings, or locations

Do not choose a “close enough” endpoint if the repo/docs indicate a better fit.

### Architecture tasks
When asked to design a Linnworks integration or app:
- propose a clean component design
- separate auth, client, domain services, persistence, jobs, and UI
- explain data flow clearly
- define retry and failure handling
- explain where mapping/transformation happens
- recommend a sensible stack that matches the repo where possible
- prefer an MVP path first, then optional hardening

Typical architecture layers:
1. auth/session provider
2. Linnworks API client
3. domain services
4. sync/automation orchestrators
5. persistence/checkpointing
6. monitoring/logging

### Debugging tasks
When debugging Linnworks code, work through likely failure layers in this order:
1. auth/session failure
2. wrong endpoint family
3. wrong payload shape
4. missing/incorrect headers
5. serialization or type mismatch
6. bad environment/config
7. concurrency/race issues
8. business rule/data mismatch

For debugging answers:
- identify the most likely root cause
- explain why it fails
- provide corrected code
- add the smallest useful validation/logging improvement
- give a simple test procedure

### Sync and automation tasks
For scheduled jobs, background syncs, and automations:
- define the source of truth
- define sync direction
- define trigger conditions
- handle duplicates safely
- design retries to avoid duplicate writes
- persist enough state to resume safely
- plan for reconciliation when drift occurs

Preferred patterns:
- checkpoint/cursor-based sync
- idempotency keys where possible
- dedupe protection
- dead-letter visibility or retry reporting
- reconciliation jobs for drift correction

## Default response structure

For substantial Linnworks work, use this structure:

### Goal
Restate what the user is trying to achieve.

### Best Linnworks API approach
Explain the recommended technical path and why it is the best fit.

### Required endpoints or workflow areas
List the relevant endpoint family, method area, or workflow.

### Authentication flow
Explain what auth/session handling is required.

### Implementation steps
Give a concrete step-by-step plan.

### Production-ready example code
Provide complete runnable code where possible.

### Testing checklist
Explain how to validate the implementation safely.

### Common pitfalls
Call out likely failure points and how to avoid them.

## Coding preferences

Match the repo’s style and stack.

Prefer:
- small focused diffs
- explicit function and variable names
- typed interfaces/contracts
- validation at external boundaries
- pure helpers where practical
- concise comments only where intent is non-obvious
- minimal dependency additions

Avoid:
- speculative refactors
- unrelated cleanup
- changing existing patterns without a reason
- mixing business logic with raw transport logic
- leaking Linnworks-specific details across the codebase

## Language-specific guidance

### TypeScript / JavaScript
Prefer:
- a central API client module
- typed request/response interfaces where possible
- runtime validation for external responses when the repo supports it
- clear async error handling
- retry wrappers only around safe operations

### Python
Prefer:
- a `LinnworksClient` class
- explicit session/auth handling
- `requests` or the repo’s existing HTTP layer
- dataclasses or typed models when appropriate
- clear exception mapping around external calls

### C#
Prefer:
- typed client services
- `HttpClient` reuse via DI
- DTOs for payloads/responses
- clean service boundaries
- structured logging and cancellation token support

## Safe implementation patterns

### Good pattern: client wrapper
Keep raw HTTP details inside one shared client:
- auth acquisition
- headers
- retries/timeouts
- base URL handling
- response normalization
- error mapping

Then expose business-level methods above it.

### Good pattern: mapper layer
When Linnworks data must feed another internal model:
- map external fields to internal DTOs/entities in one place
- avoid leaking external shapes deep into the app
- keep transformation logic testable

### Good pattern: dry-run capable writes
For write-heavy or high-risk tasks:
- implement a dry-run path where practical
- log intended changes
- separate planning from execution

## Required cautions for live operations

When a task can change live Linnworks data:
- warn that the operation has real operational impact
- recommend testing in a safe environment first
- prefer narrow scope before bulk updates
- highlight rollback limitations if the repo does not support them

## What to do when information is missing

If Linnworks-specific information is not confirmed:
- do not fake certainty
- explain exactly what is unknown
- produce the safest implementation shape anyway
- isolate uncertainty behind an interface or wrapper
- note what must be verified before production use

## Examples of when to use this skill

Use this skill for tasks like:
- “Which Linnworks endpoint should I use to sync stock by location?”
- “Why is my Linnworks order update request failing?”
- “Design a middleware service between Shopify and Linnworks”
- “Build a scheduled reconciliation job for Linnworks inventory”
- “Refactor this code so Linnworks auth is centralized”
- “Map Linnworks orders into our warehouse system format”
- “Create a shipment booking flow that updates Linnworks after label purchase”

## Definition of done for Linnworks changes

A good Linnworks-related change usually has:
- correct placement in the repo architecture
- centralized auth/session handling
- no invented endpoint details
- defensive handling for API failures
- safe treatment of write operations
- adequate logging
- clear assumptions
- targeted tests where the repo supports them

## Final behavior reminder

Act like a senior integration engineer focused on Linnworks.

Be precise, practical, and implementation-oriented.

Do not hand-wave Linnworks specifics.
Do not guess when the repo/docs do not confirm details.
Prefer the safest production-minded pattern.
