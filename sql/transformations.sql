-- ==========================================================================
-- Ruslovar API — Database Setup Script
--
-- Run this script after importing the upstream Sshra noun dump into the
-- runouns database.
--
-- It applies:
--   1. Indexes for query performance
--   2. Data fixes for known gaps in the upstream data
--   3. Adds columns for custom entries (which can be added via manager.py)
--
-- Usage:
--   mysql -u root -p runouns < setup_database.sql
-- ==========================================================================

USE runouns;

-- --------------------------------------------------------------------------
-- 1. Indexes
-- --------------------------------------------------------------------------
-- The upstream dump contains no indexes on lookup-critical columns.
-- Adding these dramatically improves query performance.

CREATE INDEX code_idx ON nouns_morf (code);
CREATE INDEX code_parent_idx ON nouns_morf (code_parent);
CREATE INDEX word_idx ON nouns_morf (word(5));

-- --------------------------------------------------------------------------
-- 2. Data fixes
-- --------------------------------------------------------------------------
-- Fix missing suppletive plural link: дети should be linked to ребенок
-- as its plural, in addition to its existing link to дитя.

-- Verify the current state before inserting:
--   SELECT * FROM nouns_morf WHERE word IN ('ребенок', 'дитя', 'дети');
--   SELECT MAX(code) FROM nouns_morf;

-- The new code must be unique. Use a value greater than the current max.
SET @new_code = (SELECT MAX(code) + 1 FROM nouns_morf);
SET @rebjonok_code = (SELECT code FROM nouns_morf WHERE word = 'ребенок' AND code_parent = 0 LIMIT 1);

INSERT INTO nouns_morf (IID, word, code, code_parent, plural, gender, wcase, soul)
VALUES (NULL, 'дети', @new_code, @rebjonok_code, 1, NULL, 'им', 1);

-- --------------------------------------------------------------------------
-- 3. Add columns for custom entries
-- --------------------------------------------------------------------------

ALTER TABLE nouns_morf
ADD COLUMN is_custom TINYINT(1) DEFAULT 0,
ADD COLUMN created_at TIMESTAMP NULL DEFAULT NULL,
ADD COLUMN category VARCHAR(50) NULL DEFAULT NULL;
