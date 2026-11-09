# PlaySmart
PlaySmart is a modular toolkit designed for data professionals to analyze game design elements, performance metrics, and team dynamics. The toolkit consists of both hardware (eye-trackers, regular and heat cameras, physiological sensors) and software (Python packages, applications) to enable modular and repeatable game analysis.


## Iteration 5 (2026–27): League of Legends capture rig

The capture toolkit has been ported from Valorant to League of Legends by the BUas
2026–27 student team. Start here:

- **Set up a capture PC:** `Toolkit setup.md` — one script (`setup.ps1`), then Tobii and OBS.
- **Prove it works:** `TESTING.md` — pre-flight check and the test sequence T1–T10.
- **What changed and why:** `CHANGELOG.md` — every modified file with its reason.

In short: one session id per game, automatic start/stop on the game itself, graceful
saving (no more truncated files), League game state from the Live Client Data API, video
driven over obs-websocket with a timing anchor, a pre-flight check, and secrets out of
the code. Since 25 Sep every field the sources already send is kept (Live Client combat
stats, client settings, post-game stats block, a per-session manifest and index), and the
upload keeps a local copy by default (`PLAYSMART_UPLOAD=keep|off|move`). The post-game
side (replay rendering, positions, Match-V5, the merge) lives in the analysis toolkit:
`github.com/BredaUniversityADSAI/2026-27s1-fai3-adsai-PlaySmart`.

**How this was built.** The iteration-5 changes were written with substantial help from
an AI assistant (Claude, Anthropic): the student set the direction, ran the hardware, the
League client and the lab sessions, reviewed and tested every change on real captures,
and applied and pushed every commit; Claude wrote most of the code and documentation to
those instructions. The reasoning per file is in `CHANGELOG.md`; the team can explain
every module and takes responsibility for it.

# Contributing to PlaySmart

Thank you for your interest in contributing to PlaySmart!

PlaySmart is an open, research-driven project aimed at improving performance, well-being, and teamwork in esports through accessible AI and data-driven tools. We welcome contributions from developers, researchers, SMEs, and practitioners.

## Our Philosophy

PlaySmart is built on the principle of **open collaboration**. While the project is released under the Apache License 2.0 to enable broad adoption (including commercial use), we strongly encourage all users to contribute improvements back to the project.

By sharing enhancements, we ensure that:
- The toolkit remains accessible and up-to-date
- Knowledge is shared across the ecosystem
- SMEs and grassroots organisations benefit from continuous innovation

## What You Can Contribute

We welcome contributions such as:
- Bug fixes
- New features or improvements
- Documentation updates
- Performance optimisations
- New use cases or applications
- Research insights and validation results

## Contribution Guidelines

1. **Fork the repository**
2. Create a new branch for your contribution
3. Clearly document your changes
4. Submit a pull request with:
   - A clear description of what was changed
   - The motivation behind the change
   - Any relevant context (e.g. use case, experiment, deployment)

## Documentation of Changes

In line with the Apache License 2.0, any modified files must clearly indicate that changes were made. Please ensure your contributions are transparent and well-documented.

## Code of Conduct

We aim to create an open, respectful, and collaborative environment. Contributions should be constructive and aligned with the goals of the project.

## Questions or Ideas?

Feel free to open an issue to:
- Ask questions
- Propose new features
- Share ideas or use cases

---

**Together, we build a healthier, smarter esports ecosystem.**
