# How it works

## Flow

1. The scheduler runs tasks on a cron plan (jobs, digest, reviews, reports).
2. Python asks headless Claude to make exact read-only Upwork tool calls
   (the bridge). Python reads the raw results. Claude does not touch the data
   path, so it cannot change anything.
3. Python filters and scores. Claude is used only where thinking or writing is
   needed: fit judging, proposals, reviews, strategy.
4. Results appear in the panel as items to approve.

## Writes

Every Upwork write is a preview first. The app calls confirm only after you
click. With dry-run ON (the default) the preview is all that ever happens.

## Learning

Memory, playbook, and skills are plain files in `/data`, kept under git
versioning so you can see changes and roll back. Weekly research may use web
search within a budget cap, and its changes need your approval.

## Cost

Python does the heavy lifting, so Claude calls stay small. The runs and costs
page shows each run and its spend. Limits and schedules are in
`config/defaults.toml` and the settings page.
