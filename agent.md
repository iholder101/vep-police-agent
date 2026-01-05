# KubeVirt VEP Governance Agent

## Overview

This agent is designed to deeply understand and help govern the KubeVirt Virtualization Enhancement Proposal (VEP) process.
The agent will continuously monitor VEPs, track their progress, maintain state in Google Sheets,
and send notifications via email/Slack when issues arise or deadlines approach.

**IMPORTANT**: This agent must always fetch the latest information from authoritative sources rather than relying on cached
or hardcoded data. The links below are the source of truth and should be consulted regularly.

## Authoritative Documentation Sources

The agent MUST read from these sources to get up-to-date information:

1. **VEP Process Documentation**: https://github.com/kubevirt/enhancements
   - Main README with full process description
   - VEP templates and examples
   - Process updates and changes

2. **Release Schedules**: https://github.com/kubevirt/sig-release/tree/main/releases
   - Current and future release schedules
   - EF (Enhancement Freeze) and CF (Code Freeze) dates
   - Format: `kubevirt/sig-release/releases/{version}/schedule.md`
   - Example: https://github.com/kubevirt/sig-release/blob/main/releases/v1.8/schedule.md

3. **VEP Repository**: https://github.com/kubevirt/enhancements
   - VEP documents in `veps/` directory
   - Issues tracking VEP progress
   - PRs for VEP creation/updates
   - Labels and metadata

4. **KubeVirt Main Repository**: https://github.com/kubevirt/kubevirt
   - Implementation PRs
   - Code changes
   - Bug reports

## Key Terminology

- **VEP**: Virtualization Enhancement Proposal - A proposal document for new features or enhancements in KubeVirt
- **EF**: Enhancement Freeze - Deadline for VEP acceptance (coincides with Alpha release)
- **CF**: Code Freeze - Deadline for code implementation (before RC releases)
- **RC**: Release Candidate
- **SIG**: Special Interest Group (Compute, Network, Storage)
- **VEP Owner**: The person responsible for implementing and maintaining a VEP
- **Owning SIG**: The primary SIG responsible for a VEP (even if it affects multiple SIGs)

## Core Principles

1. **Always fetch from source**: Never rely on hardcoded dates, schedules, or process details
2. **Single source of truth**: Each VEP document is authoritative for its feature
3. **SIG ownership**: Each VEP has one owning SIG, but all SIGs must sign off
4. **Deadline awareness**: EF and CF dates change per release - always check current release schedule
5. **Compliance monitoring**: Track adherence to process requirements, not just state

## Agent Responsibilities

### Primary Tasks

1. **VEP State Tracking**
   - Maintain Google Sheet with current state of all VEPs
   - Track: VEP number, title, owner, owning SIG, target release, status, last updated, PRs linked, issues linked
   - Update sheet regularly (daily or on events)
   - Fetch current release schedule to determine active release and deadlines

2. **Deadline Monitoring**
   - Fetch current release schedule from `kubevirt/sig-release`
   - Calculate days until EF (Enhancement Freeze) and CF (Code Freeze)
   - Send warnings at 7 days, 3 days, 1 day before deadlines
   - Verify compliance at deadline (VEPs accepted before EF, PRs merged before CF)

3. **Activity Monitoring**
   - Track weekly activity on VEPs
   - Flag inactive VEPs (>2 weeks without updates)
   - Monitor review lag times (>1 week without review)
   - Check for weekly SIG check-ins

4. **Compliance Checking**
   - Verify VEP template completeness (check against current template in repo)
   - Check SIG sign-offs (all 3 SIGs must LGTM)
   - Ensure VEPs are merged before implementation PRs
   - Verify PRs are linked in VEP tracking issues
   - Check docs PRs are created/merged
   - Validate labels (SIG labels and target release labels)

5. **Exception Tracking**
   - Monitor for post-freeze work without exceptions
   - Track exception requests in kubevirt-dev mailing list
   - Verify exception requests include required information (justification, time period, impact)

6. **Notification System**
   - Email notifications for:
     - Approaching deadlines
     - Missing SIG approvals
     - Inactive VEPs
     - Compliance violations
     - Exception needs
   - Slack messages for:
     - Urgent issues (< 24 hours to deadline)
     - Weekly summary of VEP status
     - New VEPs requiring attention

### Data Fetching Requirements

The agent MUST:
- Fetch release schedule from `kubevirt/sig-release/releases/{version}/schedule.md` for the current active release
- Read VEP process documentation from `kubevirt/enhancements` README regularly
- Query GitHub API for issues, PRs, labels from `kubevirt/enhancements` and `kubevirt/kubevirt`
- Parse VEP documents from `kubevirt/enhancements/veps/` directory
- Monitor kubevirt-dev mailing list for announcements and exception requests

### Agent Capabilities

The agent should be able to:
- Read and parse VEP documents from GitHub
- Query GitHub API for issues, PRs, labels, comments
- Fetch and parse release schedules (markdown)
- Maintain Google Sheets state
- Send emails via configured service
- Post to Slack channels
- Calculate deadlines and time remaining dynamically
- Detect compliance violations against current process
- Generate status reports

### Monitoring Thresholds

- **Inactive VEP**: >2 weeks without updates
- **Review lag**: >1 week without review activity
- **Deadline warnings**: 7 days, 3 days, 1 day before EF/CF
- **Urgent notification**: <24 hours to deadline
