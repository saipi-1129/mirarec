-- MiraRec MySQL initialization
-- This runs automatically when the MySQL container is first started

SET NAMES utf8mb4;

-- Comments table (for live comments from Mirrativ webhook integration)
CREATE TABLE IF NOT EXISTS comments (
    id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    time     DATETIME NOT NULL,
    name     VARCHAR(255) NOT NULL DEFAULT '',
    comment  TEXT NOT NULL,
    user_id  VARCHAR(64),
    live_id  VARCHAR(64),
    INDEX idx_time    (time),
    INDEX idx_user_live (user_id, live_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
