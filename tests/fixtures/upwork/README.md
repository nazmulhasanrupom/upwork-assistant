Real Upwork MCP response samples captured 2026-11-06 (personal data replaced).
Facts seen:
- Money fields are strings like "$13,692.95". Parse them.
- Many fields can be missing (client with no spend/rating, hourly with "no rate stated", no budget).
- Text from clients is wrapped in <untrusted_participant_content> tags.
- get_job: client_record has spend_total (string) and hours_total; 13692.95/1542 = 8.88 -> LOW. Has a 1-star review -> red flag. connects_cost 17 > connects_balance 3 -> red flag.
- get_job has can_apply, plan, bid_stats_available, preferred_qualifications, screening_questions.
- find_jobs args: action + org_uid + params {mode, days_posted, verified_payment_only, limit}; get uses params {job_id}.
- smart_search_best_match.json: hourly budget is the `budget` string "$15.00–$35.00/hr" (en dash). `hourly_budget_type` values seen: "client-specified range", "platform default range" (Upwork's default, NOT the client's real rate), "no rate stated". Also a `premium` flag.
- get_profile.json (action get): title/description/skills under data.personalData + data.skills; rate data.personalData.chargeRate.rawValue; profileAggregates (top_rated, totalEarnings, totalFeedback, totalJobs). No JSS field here. Text fields wrapped in untrusted tags even for own profile.
