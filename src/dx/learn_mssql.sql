/*
  dx learn — SQL Server schema (SQL Server 2017+ / Azure SQL).

  STATUS: UNQUALIFIED. This schema and MssqlStore have been exercised only
  against a fake pyodbc connection (statement shape, parameter counts,
  transaction order). They have never run against a live SQL Server. dx learn
  refuses to use this backend unless DX_LEARN_MSSQL_UNQUALIFIED=accept is set.
  Qualify it on a dev instance before relying on it.

  Working state for dx learn's recommendation queue, adoptions and decision
  audit, plus per-pass fact snapshots for reporting. This is NOT the ledger:
  the devswarm ledger stays the system of record for what was merged and who
  signed it, and dx learn never writes to it (RL-008).

  Idempotent: safe to re-run. Run as a schema owner, then grant the dx learn
  login only what MssqlStore uses:

    GRANT SELECT, INSERT, UPDATE ON SCHEMA::dx_learn TO [dx_learn_app];
    DENY  UPDATE, DELETE ON dx_learn.DecisionAudit TO [dx_learn_app];
*/
IF SCHEMA_ID(N'dx_learn') IS NULL EXEC (N'CREATE SCHEMA dx_learn AUTHORIZATION dbo;');
GO

IF OBJECT_ID(N'dx_learn.Recommendation', N'U') IS NULL
CREATE TABLE dx_learn.Recommendation (
    RecKey           CHAR(24)          NOT NULL CONSTRAINT PK_Recommendation PRIMARY KEY,
    Revision         INT               NOT NULL,   -- optimistic concurrency: bumped on every write
    Kind             VARCHAR(40)       NOT NULL
        CONSTRAINT CK_Recommendation_Kind CHECK (Kind IN ('recurring_failure','variant_preference',
            'gate_regression','human_rejection_theme','reviewer_lane','operator_failure',
            'adoption_ineffective')),
    Subject          NVARCHAR(200)     NOT NULL,
    Score            FLOAT             NOT NULL,
    EvidenceWeight   FLOAT             NOT NULL,
    EvidenceJson     NVARCHAR(MAX)     NOT NULL CONSTRAINT CK_Recommendation_Evidence CHECK (ISJSON(EvidenceJson) = 1),
    DraftText        NVARCHAR(MAX)     NOT NULL,
    MetricJson       NVARCHAR(400)     NOT NULL CONSTRAINT CK_Recommendation_Metric CHECK (ISJSON(MetricJson) = 1),
    TargetsJson      NVARCHAR(MAX)     NOT NULL CONSTRAINT DF_Recommendation_Targets DEFAULT (N'[]'),
    SuggestedRole    VARCHAR(40)       NOT NULL,
    Status           VARCHAR(10)       NOT NULL
        CONSTRAINT CK_Recommendation_Status CHECK (Status IN ('PROPOSED','ACCEPTED','REJECTED','RETIRED','STALE')),
    CreatedAt        DATETIMEOFFSET(3) NULL,
    UpdatedAt        DATETIMEOFFSET(3) NULL,
    DecidedBy        NVARCHAR(100)     NULL,
    DecidedAt        DATETIMEOFFSET(3) NULL,
    DecisionNote     NVARCHAR(2000)    NULL,
    WeightAtDecision FLOAT             NULL,
    PxxSlug          CHAR(12)          NULL,
    -- A decided recommendation always names who decided it.
    CONSTRAINT CK_Recommendation_Decided CHECK (
        Status NOT IN ('ACCEPTED','REJECTED','RETIRED') OR (DecidedBy IS NOT NULL AND DecidedAt IS NOT NULL))
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Recommendation_Status_Score')
    CREATE INDEX IX_Recommendation_Status_Score ON dx_learn.Recommendation (Status, Score DESC) INCLUDE (Kind, Subject);
GO

IF OBJECT_ID(N'dx_learn.Adoption', N'U') IS NULL
CREATE TABLE dx_learn.Adoption (
    RecKey      CHAR(24)          NOT NULL CONSTRAINT PK_Adoption PRIMARY KEY
                CONSTRAINT FK_Adoption_Recommendation REFERENCES dx_learn.Recommendation (RecKey),
    Revision    INT               NOT NULL,
    MetricJson  NVARCHAR(400)     NOT NULL CONSTRAINT CK_Adoption_Metric CHECK (ISJSON(MetricJson) = 1),
    AcceptedBy  NVARCHAR(100)     NOT NULL,
    AcceptedAt  DATETIMEOFFSET(3) NOT NULL,
    Note        NVARCHAR(2000)    NOT NULL CONSTRAINT DF_Adoption_Note DEFAULT (N''),
    TaskId      NVARCHAR(64)      NULL,     -- the dx task implementing it; efficacy starts at its MERGED row
    Active      BIT               NOT NULL CONSTRAINT DF_Adoption_Active DEFAULT (1),
    RetiredAt   DATETIMEOFFSET(3) NULL
);
GO

-- dx's durable record of each forwarding into pxx's improvement inbox.
IF OBJECT_ID(N'dx_learn.Forward', N'U') IS NULL
CREATE TABLE dx_learn.Forward (
    RecKey        CHAR(24)          NOT NULL CONSTRAINT PK_Forward PRIMARY KEY
                  CONSTRAINT FK_Forward_Recommendation REFERENCES dx_learn.Recommendation (RecKey),
    Revision      INT               NOT NULL,
    Slug          CHAR(12)          NOT NULL,
    PayloadSha256 CHAR(64)          NOT NULL,
    State         VARCHAR(12)       NOT NULL
        CONSTRAINT CK_Forward_State CHECK (State IN ('INTENT','WRITTEN','NOT_WRITTEN','INTERRUPTED','WITHDRAWN')),
    RecRevision   INT               NOT NULL,
    UpdatedAt     DATETIMEOFFSET(3) NOT NULL,
    PxxView       VARCHAR(16)       NULL,
    Divergence    NVARCHAR(400)     NULL,
    Note          NVARCHAR(2000)    NOT NULL CONSTRAINT DF_Forward_Note DEFAULT (N'')
);
GO

-- Append-only decision audit. The trigger is the guard; the DENY above is the second one.
IF OBJECT_ID(N'dx_learn.DecisionAudit', N'U') IS NULL
CREATE TABLE dx_learn.DecisionAudit (
    AuditId    BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DecisionAudit PRIMARY KEY,
    Ts         DATETIMEOFFSET(3)    NOT NULL,
    Actor      NVARCHAR(100)        NOT NULL,
    Action     VARCHAR(20)          NOT NULL,
    RecKey     CHAR(24)             NOT NULL,
    -- The exact canonical entry that was hashed; Hash = sha256(EntryJson) and
    -- EntryJson.prev_hash = the previous row's Hash. dx learn verifies the chain
    -- before every decision.
    EntryJson  NVARCHAR(MAX)        NOT NULL CONSTRAINT CK_DecisionAudit_Entry CHECK (ISJSON(EntryJson) = 1),
    PrevHash   CHAR(64)             NOT NULL,
    Hash       CHAR(64)             NOT NULL CONSTRAINT UQ_DecisionAudit_Hash UNIQUE
);
GO
CREATE OR ALTER TRIGGER dx_learn.TR_DecisionAudit_AppendOnly
ON dx_learn.DecisionAudit
INSTEAD OF UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;
    THROW 51000, N'dx_learn.DecisionAudit is append-only: append a correcting decision instead.', 1;
END;
GO

IF OBJECT_ID(N'dx_learn.LearnPass', N'U') IS NULL
CREATE TABLE dx_learn.LearnPass (
    PassId      BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_LearnPass PRIMARY KEY,
    Ts          DATETIMEOFFSET(3)    NOT NULL,
    LedgerHead  CHAR(64)             NULL,      -- the verified ledger head this pass learned from
    SummaryJson NVARCHAR(MAX)        NOT NULL CONSTRAINT CK_LearnPass_Summary CHECK (ISJSON(SummaryJson) = 1)
);
GO

-- Fact snapshots, one set per pass, for reporting. Read the latest pass, or
-- trend across passes; they are copies, the source records stay authoritative.
IF OBJECT_ID(N'dx_learn.Attempt', N'U') IS NULL
CREATE TABLE dx_learn.Attempt (
    PassId         BIGINT            NOT NULL CONSTRAINT FK_Attempt_Pass REFERENCES dx_learn.LearnPass (PassId),
    RunId          VARCHAR(64)       NOT NULL,
    Ts             DATETIMEOFFSET(3) NOT NULL,
    TaskKey        NVARCHAR(64)      NOT NULL,
    DxTaskId       NVARCHAR(64)      NULL,
    TaskClass      NVARCHAR(200)     NOT NULL,
    Model          NVARCHAR(200)     NOT NULL,
    AgentVersionId VARCHAR(32)       NOT NULL,
    Code           VARCHAR(40)       NOT NULL,
    Success        BIT               NOT NULL,
    CONSTRAINT PK_Attempt PRIMARY KEY (PassId, RunId)
);
GO

IF OBJECT_ID(N'dx_learn.LedgerEvent', N'U') IS NULL
CREATE TABLE dx_learn.LedgerEvent (
    PassId      BIGINT            NOT NULL CONSTRAINT FK_LedgerEvent_Pass REFERENCES dx_learn.LearnPass (PassId),
    EventNo     BIGINT IDENTITY(1,1) NOT NULL,
    Ts          DATETIMEOFFSET(3) NOT NULL,
    TaskId      NVARCHAR(64)      NOT NULL,
    Action      VARCHAR(20)       NOT NULL,
    SignatureId CHAR(16)          NULL,
    CONSTRAINT PK_LedgerEvent PRIMARY KEY (PassId, EventNo)
);
GO

/* Reporting view: first-pass yield by task class and model for the latest pass. */
CREATE OR ALTER VIEW dx_learn.vYieldLatest AS
SELECT a.TaskClass, a.Model,
       COUNT(*)                                     AS Runs,
       SUM(CAST(a.Success AS INT))                  AS Completed,
       CAST(AVG(CAST(a.Success AS FLOAT)) AS DECIMAL(5,4)) AS CompletedRate
FROM dx_learn.Attempt AS a
WHERE a.PassId = (SELECT MAX(PassId) FROM dx_learn.LearnPass)
GROUP BY a.TaskClass, a.Model;
GO
