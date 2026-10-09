-- Every number in the demo, as SQL on the claude_code traces stream in OpenObserve.
-- Run in Logs/Traces search with stream type "traces"; durations are microseconds.

-- Overview: sessions, prompts, average and longest prompt
SELECT count(DISTINCT session_id) AS sessions, count(*) AS prompts,
       round(avg(duration)/1e6) AS avg_s, round(max(duration)/1e6) AS longest_s
FROM claude_code WHERE operation_name = 'claude_code.interaction';

-- Permission waits by who decided (config = allow-rules, user_temporary = a human click)
SELECT source, decision, count(*) AS waits,
       round(sum(duration)/60e6) AS minutes, round(avg(duration)/1e6, 1) AS avg_s,
       round(max(duration)/60e6) AS longest_min
FROM claude_code WHERE operation_name = 'claude_code.tool.blocked_on_user'
GROUP BY source, decision ORDER BY minutes DESC;

-- Per tool call: executing vs waiting for permission
SELECT operation_name, round(avg(duration)/1e6, 2) AS avg_s
FROM claude_code
WHERE operation_name IN ('claude_code.tool.execution', 'claude_code.tool.blocked_on_user')
GROUP BY operation_name;

-- Prompt-cache hit rate, p95 time to first token
SELECT round(100.0 * sum(CAST(cache_read_tokens AS BIGINT)) /
             (sum(CAST(cache_read_tokens AS BIGINT)) + sum(CAST(input_tokens AS BIGINT)) +
              sum(CAST(cache_creation_tokens AS BIGINT))), 1) AS cache_hit_pct,
       round(approx_percentile_cont(CAST(ttft_ms AS DOUBLE), 0.95)) AS p95_ttft_ms
FROM claude_code WHERE operation_name = 'claude_code.llm_request';

-- Where tool time goes
SELECT tool_name, count(*) AS calls, round(sum(duration)/60e6) AS minutes
FROM claude_code WHERE operation_name = 'claude_code.tool'
GROUP BY tool_name ORDER BY minutes DESC LIMIT 10;

-- Biggest traces (multi-agent runs show up here)
SELECT trace_id, count(*) AS spans, count(DISTINCT agent_id) AS subagents,
       round((max(end_time) - min(start_time)) / 60e9, 1) AS minutes
FROM claude_code GROUP BY trace_id ORDER BY spans DESC LIMIT 10;

-- Tool failures by class
SELECT error_class, count(*) AS failures
FROM claude_code
WHERE operation_name = 'claude_code.tool.execution' AND success = 'false'
GROUP BY error_class ORDER BY failures DESC;

-- Spend (events stream: switch stream type to "logs")
-- SELECT round(sum(CAST(cost_usd AS DOUBLE)), 2) AS usd FROM claude_code WHERE event_name = 'api_request';
