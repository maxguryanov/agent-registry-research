-- ---------------------------------------------------------------------------
-- Continuous monitoring schema for the ERC-8004 Identity Registry on Base.
--
-- Design rules:
--   * Every statement is idempotent. Running this file twice is a no-op.
--   * agents          = current state, one row per agent, overwritten in place.
--   * registration_events = raw chain logs, append-only, never updated.
--   * liveness_checks = one row per (agent, probe run). Nothing is overwritten.
--                       Survival analysis reads this table.
--   * The indexer cursor lives in indexer_state, not in a file.
--
-- Apply with:  python3 -m monitor.init_db
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version     INT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note        TEXT
);


-- ---------------------------------------------------------------------------
-- Block timestamp cache.
--
-- eth_getBlockByNumber is a separate RPC round trip per block. Events cluster
-- into the same blocks, and blocks are immutable once confirmed, so caching
-- turns thousands of calls into dozens.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS blocks (
    block_number    BIGINT PRIMARY KEY,
    block_time      TIMESTAMPTZ NOT NULL
);


-- ---------------------------------------------------------------------------
-- Raw events. Append-only.
--
-- The UNIQUE constraint on (tx_hash, log_index) is what makes re-indexing a
-- block range safe: a duplicate insert is rejected by the database itself,
-- not by application logic that might have a bug in it.
--
-- raw_data keeps the untouched hex payload. If a decoder turns out to be wrong
-- for some event type, the events can be re-decoded from this column without
-- walking the chain again.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS registration_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT          NOT NULL,   -- Registered | URIUpdated | Transfer
                                              -- | MetadataSet | unknown
    agent_id        NUMERIC(78,0),            -- uint256; NULL if the log had no agent id
    block_number    BIGINT        NOT NULL,
    block_time      TIMESTAMPTZ,
    tx_hash         TEXT          NOT NULL,
    log_index       INT           NOT NULL,
    topic0          TEXT          NOT NULL,   -- event signature hash, as seen on chain

    owner           TEXT,                     -- Registered: owner. Transfer: recipient.
    from_address    TEXT,                     -- Transfer only: previous holder
    agent_uri       TEXT,                     -- Registered / URIUpdated: decoded string
    metadata_key    TEXT,                     -- MetadataSet only, e.g. 'agentWallet'
    metadata_value  TEXT,                     -- MetadataSet only, hex or utf-8

    raw_topics      TEXT,                     -- JSON array of topic hex strings
    raw_data        TEXT,                     -- untouched ABI-encoded data payload
    ingested_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT registration_events_unique_log UNIQUE (tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS registration_events_agent_order_idx
    ON registration_events (agent_id, block_number, log_index);
CREATE INDEX IF NOT EXISTS registration_events_block_idx
    ON registration_events (block_number);
CREATE INDEX IF NOT EXISTS registration_events_type_idx
    ON registration_events (event_type);
CREATE INDEX IF NOT EXISTS registration_events_metadata_key_idx
    ON registration_events (metadata_key) WHERE metadata_key IS NOT NULL;

-- Added after v1. Written this way so the file stays safe to re-run against a
-- database created before these columns existed.
ALTER TABLE registration_events ADD COLUMN IF NOT EXISTS metadata_key   TEXT;
ALTER TABLE registration_events ADD COLUMN IF NOT EXISTS metadata_value TEXT;


-- ---------------------------------------------------------------------------
-- Current state, derived from the events above.
--
-- current_uri is the value the prober must use. It comes from the most recent
-- URIUpdated event, falling back to the registration URI. Probing the
-- registration URI instead overstates the dead population by roughly a third,
-- because 16.5% of agents change their URI after minting.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agents (
    agent_id                NUMERIC(78,0) PRIMARY KEY,

    owner                   TEXT,             -- current holder, lowercase hex
    minted_to               TEXT,             -- holder at registration, lowercase hex
    transfer_count          INT           NOT NULL DEFAULT 0,

    registered_block        BIGINT        NOT NULL,
    registered_at           TIMESTAMPTZ,
    registered_tx           TEXT,

    uri_at_registration     TEXT          NOT NULL DEFAULT '',
    current_uri             TEXT          NOT NULL DEFAULT '',
    current_uri_block       BIGINT,           -- block of the event that set current_uri
    current_uri_log_index   INT,
    uri_change_count        INT           NOT NULL DEFAULT 0,

    -- Filled by the prober / metrics stage, used for clustering agents into
    -- projects by shared root domain.
    uri_host                TEXT,
    uri_root_domain         TEXT,

    -- Panel membership. See the note in the prober module: probing all 60k+
    -- agents every day is neither affordable nor polite, so survival is
    -- measured on a fixed panel that is re-checked daily.
    in_panel                BOOLEAN       NOT NULL DEFAULT FALSE,
    panel_added_at          TIMESTAMPTZ,
    panel_stratum           TEXT,

    first_seen_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agents_owner_idx           ON agents (owner);
CREATE INDEX IF NOT EXISTS agents_registered_at_idx   ON agents (registered_at);
CREATE INDEX IF NOT EXISTS agents_root_domain_idx     ON agents (uri_root_domain);
CREATE INDEX IF NOT EXISTS agents_panel_idx           ON agents (in_panel) WHERE in_panel;


-- ---------------------------------------------------------------------------
-- Probe runs.
--
-- Concurrency, timeout and prober version are recorded per run on purpose.
-- Raising the timeout does not remove failures, it reclassifies them:
-- ReadTimeout becomes HTTP 504 from an IPFS gateway. Two runs are only
-- comparable if you can see the settings each one used.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS check_runs (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT          NOT NULL DEFAULT 'panel',   -- panel | sweep | adhoc
    status          TEXT          NOT NULL DEFAULT 'running', -- running | completed | failed
    started_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,

    agents_planned  INT,
    agents_checked  INT,

    concurrency     INT,
    timeout_seconds NUMERIC(6,2),
    user_agent      TEXT,
    prober_version  TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS check_runs_started_idx ON check_runs (started_at DESC);


-- ---------------------------------------------------------------------------
-- One row per agent per run. Never updated, never deleted.
--
-- The six funnel steps are stored as separate booleans rather than a single
-- "stage" number so that a change in how liveness is defined can be recomputed
-- from history instead of requiring a re-probe.
--
-- Failure classification is stored apart from the pass/fail flags for the same
-- reason: a failure moving from `timeout` to `http_504_gateway` is a fact about
-- the measurement, not about the agent.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS liveness_checks (
    id                      BIGSERIAL PRIMARY KEY,
    run_id                  BIGINT        NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
    agent_id                NUMERIC(78,0) NOT NULL,
    checked_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),

    uri_checked             TEXT          NOT NULL DEFAULT '',
    uri_source              TEXT          NOT NULL DEFAULT 'current_uri',
    uri_scheme              TEXT,
    resolved_url            TEXT,

    -- The funnel.
    s1_uri_present          BOOLEAN       NOT NULL DEFAULT FALSE,
    s2_resolved             BOOLEAN       NOT NULL DEFAULT FALSE,
    s3_valid_json           BOOLEAN       NOT NULL DEFAULT FALSE,
    s4_schema_match         BOOLEAN       NOT NULL DEFAULT FALSE,
    s5_has_services         BOOLEAN       NOT NULL DEFAULT FALSE,
    s6_endpoint_alive       BOOLEAN       NOT NULL DEFAULT FALSE,
    live_strict             BOOLEAN       NOT NULL DEFAULT FALSE,
    funnel_stage            SMALLINT      NOT NULL DEFAULT 0,

    -- Schema conformance, both criteria kept separate.
    -- The only canonical type value observed in the data is
    -- https://eips.ethereum.org/EIPS/eip-8004#registration-v1 , and
    -- registrations[].agentRegistry appears in well under a fifth of valid
    -- documents. Neither test alone is sufficient, so both are recorded.
    type_field_raw          TEXT,
    type_is_canonical       BOOLEAN       NOT NULL DEFAULT FALSE,
    registry_field_present  BOOLEAN       NOT NULL DEFAULT FALSE,
    registry_field_matches  BOOLEAN       NOT NULL DEFAULT FALSE,

    -- Failure taxonomy.
    failure_stage           SMALLINT,
    failure_category        TEXT,
    failure_detail          TEXT,
    http_status             INT,
    latency_ms              INT,
    content_type            TEXT,
    content_bytes           INT,

    -- Endpoints.
    services_count          INT           NOT NULL DEFAULT 0,
    endpoints_total         INT           NOT NULL DEFAULT 0,
    endpoints_checked       INT           NOT NULL DEFAULT 0,
    endpoints_ok            INT           NOT NULL DEFAULT 0,
    endpoints_ok_specific   INT           NOT NULL DEFAULT 0,  -- generic hosts excluded
    generic_only            BOOLEAN       NOT NULL DEFAULT FALSE,
    endpoint_details        JSONB,

    doc_sha256              TEXT,         -- detects a document changing between runs

    CONSTRAINT liveness_checks_one_per_run UNIQUE (run_id, agent_id)
);

CREATE INDEX IF NOT EXISTS liveness_checks_agent_time_idx
    ON liveness_checks (agent_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS liveness_checks_time_idx
    ON liveness_checks (checked_at);
CREATE INDEX IF NOT EXISTS liveness_checks_run_idx
    ON liveness_checks (run_id);
CREATE INDEX IF NOT EXISTS liveness_checks_live_idx
    ON liveness_checks (checked_at) WHERE live_strict;


-- ---------------------------------------------------------------------------
-- Indexer cursor. One row per event stream being followed.
--
-- last_block is the highest block whose logs are fully ingested. The indexer
-- resumes from last_block + 1 and stops `confirmations` blocks short of the
-- chain tip, so a short reorg cannot leave a phantom event behind.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS indexer_state (
    stream              TEXT PRIMARY KEY,
    chain_id            INT           NOT NULL,
    contract            TEXT          NOT NULL,
    start_block         BIGINT        NOT NULL,
    last_block          BIGINT        NOT NULL,
    confirmations       INT           NOT NULL DEFAULT 50,

    last_run_at         TIMESTAMPTZ,
    last_run_status     TEXT,
    last_error          TEXT,
    events_ingested     BIGINT        NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Seed the Base Identity Registry stream. last_block = start_block - 1 means
-- "nothing ingested yet"; the first indexer run begins at the deploy block.
INSERT INTO indexer_state (stream, chain_id, contract, start_block, last_block)
VALUES (
    'identity_registry_base',
    8453,
    '0x8004a169fb4a3325136eb29fa0ceb6d2e539a432',
    41453265,
    41453264
)
ON CONFLICT (stream) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Hosts to stay away from.
--
-- The prober identifies itself with a contact address. That is only worth
-- anything if a request to stop can actually be honoured, so this table is the
-- mechanism behind the promise:
--
--   INSERT INTO excluded_hosts (host, reason, requested_by)
--   VALUES ('example.com', 'operator asked by email', 'ops@example.com');
--
-- Matching covers subdomains. Excluded agents are still checked and recorded,
-- with failure_category 'excluded_by_request', so they can be told apart from
-- agents that are actually dead.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS excluded_hosts (
    host            TEXT PRIMARY KEY,
    reason          TEXT,
    requested_by    TEXT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Convenience view: the most recent check for each agent.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW latest_liveness AS
SELECT DISTINCT ON (agent_id) *
FROM liveness_checks
ORDER BY agent_id, checked_at DESC, id DESC;


INSERT INTO schema_version (version, note)
VALUES (1, 'initial monitoring schema'),
       (2, 'MetadataSet key/value columns'),
       (3, 'excluded_hosts')
ON CONFLICT (version) DO NOTHING;
