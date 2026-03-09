# PartyHat — Agents

> Bringing the next 1 million builders to the Avalanche ecosystem.

PartyHat is an AI-powered smart contract IDE that lets non-technical founders and autonomous agents go from idea to a fully deployed, verified Avalanche smart contract through a simple conversation.

This repository contains the **agent layer and backend API** for PartyHat. The frontend lives in a separate repository.

---

## Mission

The barrier to deploying smart contracts on Avalanche is not just technical complexity; it is the gap between having an idea and knowing how to express it in code. PartyHat closes that gap entirely.

A user describes what they want to build in plain language. PartyHat plans it, writes production Solidity, runs Foundry tests, and deploys it to Avalanche; without the user writing a single line of code. Every step runs in a project-scoped siloed sandbox on PartyHat's cloud infrastructure.

PartyHat is also built to be consumed by other agents. The full pipeline is exposed as an MCP server, meaning any external AI agent can inherit PartyHat's capabilities and deploy Avalanche smart contracts programmatically.

---

## Architecture

PartyHat is organised into four layers.

### 1. Backend (FastAPI + Neon Postgres)

The main entry point. Manages users and projects, validates ownership, sets request-scoped context, and streams agent responses to the frontend via Server-Sent Events (SSE).

### 2. Agent Layer (LangGraph + DeepAgents)

Five specialised AI agents, each with a dedicated role, tool set, system prompt, and isolated memory slice. No agent can read or modify the state of another.

| Agent          | Role                                                                                                                                                                                                                        |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Planning**   | Collects requirements conversationally, one question at a time. Produces a structured plan with human-readable conditions. Acts as the pipeline initialiser.                                                                |
| **Coding**     | Generates production Solidity from the approved plan. Connected to OpenZeppelin and Chainlink MCP servers. It automatically incorporates standard templates, data feeds, or custom oracles when the contract requires them. |
| **Testing**    | Writes and executes Foundry tests inside a project-scoped siloed sandbox on Modal. Loops autonomously until all tests pass.                                                                                                 |
| **Deployment** | Signs and broadcasts the contract to the target Avalanche network, calls constructors, awaits confirmation, and submits for Snowtrace verification. Retries as needed.                                                      |
| **Audit**      | Continuously tracks and flags security vulnerabilities across the pipeline.                                                                                                                                                 |

**Pipeline lifecycle:**

```
draft → ready → generating → testing → deployed
```

The lifecycle is flexible across all pre-deployment stages: users can move back and forth freely. Once deployed, contracts are immutable by design, enforced at the API layer.

### 3. Memory: Hot/Cold Split

Agent context windows are kept intentionally small. Letta holds only lean working-state pointers per project. All durable data lives in Neon Postgres.

| Layer                    | Contents                                                                                        |
| ------------------------ | ----------------------------------------------------------------------------------------------- |
| **Letta (hot)**          | Working-state pointers per project (~500 tokens max) — plan, last test result, deployed address |
| **Neon Postgres (cold)** | All durable data — full plans, reasoning notes, agent logs, test results, deployment records    |
| **Modal volumes**        | Generated contract artifacts                                                                    |

### 4. On-Chain Layer (Avalanche)

PartyHat targets the full Avalanche ecosystem. Fuji serves as the default testnet, with Avalanche C-Chain and custom subnets supported for production deployments. All on-chain interactions, signing, broadcasting, confirmation, and Snowtrace verification, are handled autonomously by the Deployment agent.

---

## MCP Server

PartyHat exposes the full pipeline as an MCP server over SSE transport, enabling external AI agents to build and deploy Avalanche smart contracts programmatically. Each tool call supports x402 micropayments on Avalanche.

| Tool                         | Description                                 |
| ---------------------------- | ------------------------------------------- |
| `partyhat_start_planning`    | Start or continue a planning conversation   |
| `partyhat_generate_contract` | Generate Solidity from the approved plan    |
| `partyhat_run_tests`         | Run Foundry tests in a siloed sandbox       |
| `partyhat_deploy_contract`   | Deploy to Avalanche and verify on Snowtrace |
| `partyhat_audit_contract`    | Run a security audit on generated contracts |

---

## Tech Stack

| Layer                       | Technology                                 |
| --------------------------- | ------------------------------------------ |
| Agent orchestration         | LangGraph + DeepAgents                     |
| LLM                         | GPT-5.2 (`gpt-5.2-2025-12-11`)             |
| Working memory              | Letta                                      |
| Durable storage             | Neon Postgres (SQLAlchemy async + asyncpg) |
| Artifact storage            | Modal cloud volumes                        |
| API                         | FastAPI with SSE streaming                 |
| MCP server                  | FastMCP (SSE transport)                    |
| MCP integrations            | OpenZeppelin, Chainlink                    |
| Smart contract testing      | Foundry                                    |
| Smart contract verification | Snowtrace                                  |
| Target chain                | Avalanche (Fuji, C-Chain, custom subnets)  |
| Authentication              | Privy (wallet-based)                       |
| Package manager             | uv                                         |

---

## Project Structure

```
agents/
├── agents/
│   ├── agent_registry.py      # Intent router
│   ├── planning_agent.py      # Planning agent + LangGraph graph
│   ├── planning_tools.py      # Planning agent tools
│   ├── coding_tools.py        # Coding agent tools
│   ├── testing_tools.py       # Testing agent tools
│   ├── deployment_tools.py    # Deployment agent tools
│   ├── audit_tools.py         # Audit agent tools
│   ├── memory_manager.py      # Hot/cold memory split (Letta + Neon)
│   ├── code_storage.py        # Artifact storage
│   ├── context.py             # Request-scoped project/user context
│   └── db/
│       ├── models.py          # SQLAlchemy models
│       ├── crud.py            # Database operations
│       └── __init__.py        # Async session factory
├── partyhat_mcp/
│   ├── server.py              # FastMCP server (SSE, port 8001)
│   ├── tools.py               # MCP tool handlers
│   └── auth.py                # x402 payment verification hook
├── schemas/                   # Pydantic schemas
├── tests/                     # Agent wiring tests
├── api.py                     # FastAPI app (port 8000)
└── pyproject.toml
```

---
