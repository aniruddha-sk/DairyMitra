"""
DairyMitra 2.0 — FAT/SNF workflow.

Additive only: no existing table or column is dropped or renamed, and no
existing row is modified. Column type changes are limited to the FAT/SNF
tables (UNSIGNED INT -> INT) so they can carry foreign keys to users/vendors,
whose keys are signed INT.
"""
from migrations.runner import Step

VERSION = "001"
DESCRIPTION = "FAT/SNF 2.0: chart settings & versions, entry status, idempotency, audit log, tenant FKs"


def _col(table, column, ddl):
    return Step(
        description=f"add {table}.{column}",
        sql=f"ALTER TABLE `{table}` ADD COLUMN {ddl}",
        applied=lambda s: s.column_exists(table, column),
    )


def _to_signed(table, column, ddl):
    return Step(
        description=f"{table}.{column} -> signed INT (FK compatible)",
        sql=f"ALTER TABLE `{table}` MODIFY COLUMN {ddl}",
        applied=lambda s: s.column_type(table, column) == "int",
    )


def _index(table, name, cols, unique=False):
    kind = "UNIQUE INDEX" if unique else "INDEX"
    return Step(
        description=f"index {table}.{name}",
        sql=f"ALTER TABLE `{table}` ADD {kind} `{name}` ({cols})",
        applied=lambda s: s.index_exists(table, name),
    )


def _fk(table, name, clause, orphan_sql):
    def precondition(s):
        n = s.count(orphan_sql)
        if n:
            return f"{n} orphan row(s) found; FK not added, data left untouched"
        return None

    return Step(
        description=f"foreign key {table}.{name}",
        sql=f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` {clause}",
        applied=lambda s: s.fk_exists(table, name),
        precondition=precondition,
    )


STEPS = [
    # ---------------------------------------------------------------
    # Supporting tables that exist in production but may be missing in
    # older environments (definitions identical to schema.sql).
    # ---------------------------------------------------------------
    Step(
        description="ensure dm_challenges",
        sql="""CREATE TABLE IF NOT EXISTS `dm_challenges` (
  `id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `purpose` varchar(20) NOT NULL,
  `account_key` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `otp_hash` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `payload` text NOT NULL,
  `expires_at` bigint NOT NULL,
  `attempts` int NOT NULL DEFAULT '0',
  `consumed` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_dm_challenge_account` (`account_key`,`purpose`),
  KEY `ix_dm_challenge_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        applied=lambda s: s.table_exists("dm_challenges"),
    ),
    Step(
        description="ensure dm_rate_limits",
        sql="""CREATE TABLE IF NOT EXISTS `dm_rate_limits` (
  `bucket_key` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `hits` int NOT NULL,
  `expires_at` bigint NOT NULL,
  PRIMARY KEY (`bucket_key`),
  KEY `ix_dm_limit_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        applied=lambda s: s.table_exists("dm_rate_limits"),
    ),
    Step(
        description="create dm_audit_log",
        sql="""CREATE TABLE `dm_audit_log` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `actor_role` varchar(10) NOT NULL,
  `actor_id` int DEFAULT NULL,
  `action` varchar(60) NOT NULL,
  `target_type` varchar(40) DEFAULT NULL,
  `target_id` varchar(64) DEFAULT NULL,
  `old_values` json DEFAULT NULL,
  `new_values` json DEFAULT NULL,
  `request_id` varchar(32) DEFAULT NULL,
  `ip` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_audit_tenant_time` (`user_id`,`created_at`),
  KEY `ix_audit_target` (`target_type`,`target_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        applied=lambda s: s.table_exists("dm_audit_log"),
    ),

    # ---------------------------------------------------------------
    # Calculation mode settings: audit metadata + warning thresholds
    # ---------------------------------------------------------------
    _to_signed("fat_snf_calculation_settings", "user_id", "`user_id` INT NOT NULL"),
    _col("fat_snf_calculation_settings", "changed_by_role",
         "`changed_by_role` varchar(10) DEFAULT NULL"),
    _col("fat_snf_calculation_settings", "warn_low_litres",
         "`warn_low_litres` decimal(10,2) NOT NULL DEFAULT '0.50'"),
    _col("fat_snf_calculation_settings", "warn_high_litres",
         "`warn_high_litres` decimal(10,2) NOT NULL DEFAULT '100.00'"),
    _col("fat_snf_calculation_settings", "warn_deviation_pct",
         "`warn_deviation_pct` int NOT NULL DEFAULT '60'"),

    # ---------------------------------------------------------------
    # Rate charts: generation settings, versions, activation history
    # ---------------------------------------------------------------
    _to_signed("fat_snf_charts", "user_id", "`user_id` INT NOT NULL"),
    _to_signed("fat_snf_charts", "created_by_staff_id", "`created_by_staff_id` INT DEFAULT NULL"),
    _col("fat_snf_charts", "fat_min", "`fat_min` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "fat_max", "`fat_max` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "fat_step", "`fat_step` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "snf_min", "`snf_min` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "snf_max", "`snf_max` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "snf_step", "`snf_step` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "std_fat", "`std_fat` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "std_snf", "`std_snf` decimal(5,2) DEFAULT NULL"),
    _col("fat_snf_charts", "std_rate", "`std_rate` decimal(10,2) DEFAULT NULL"),
    _col("fat_snf_charts", "fat_rate_step", "`fat_rate_step` decimal(10,2) DEFAULT NULL"),
    _col("fat_snf_charts", "snf_rate_step", "`snf_rate_step` decimal(10,2) DEFAULT NULL"),
    _col("fat_snf_charts", "version", "`version` int NOT NULL DEFAULT '1'"),
    _col("fat_snf_charts", "revised_from_chart_id",
         "`revised_from_chart_id` int unsigned DEFAULT NULL"),
    _col("fat_snf_charts", "activated_at", "`activated_at` timestamp NULL DEFAULT NULL"),
    _col("fat_snf_charts", "deactivated_at", "`deactivated_at` timestamp NULL DEFAULT NULL"),

    # ---------------------------------------------------------------
    # Collection entries: status (saved -> final) and idempotency key
    # ---------------------------------------------------------------
    _to_signed("fat_snf_milk_collection", "user_id", "`user_id` INT NOT NULL"),
    _to_signed("fat_snf_milk_collection", "vendor_id", "`vendor_id` INT NOT NULL"),
    _to_signed("fat_snf_milk_collection", "staff_id", "`staff_id` INT DEFAULT NULL"),
    _col("fat_snf_milk_collection", "status",
         "`status` enum('saved','final') NOT NULL DEFAULT 'saved'"),
    _col("fat_snf_milk_collection", "finalized_at",
         "`finalized_at` timestamp NULL DEFAULT NULL"),
    _col("fat_snf_milk_collection", "client_request_id",
         "`client_request_id` varchar(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL"),
    _index("fat_snf_milk_collection", "uq_fat_snf_collection_request",
           "`user_id`,`client_request_id`", unique=True),
    _index("fat_snf_milk_collection", "idx_fat_snf_collection_status",
           "`user_id`,`collection_date`,`status`"),

    # ---------------------------------------------------------------
    # Tenant / integrity foreign keys (skipped, never forced, on orphans)
    # ---------------------------------------------------------------
    _fk("fat_snf_calculation_settings", "fk_fsn_settings_user",
        "FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE",
        "SELECT COUNT(*) FROM fat_snf_calculation_settings s "
        "LEFT JOIN users u ON u.id = s.user_id WHERE u.id IS NULL"),
    _fk("fat_snf_charts", "fk_fsn_chart_user",
        "FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE",
        "SELECT COUNT(*) FROM fat_snf_charts c "
        "LEFT JOIN users u ON u.id = c.user_id WHERE u.id IS NULL"),
    _fk("fat_snf_chart_values", "fk_fsn_value_chart",
        "FOREIGN KEY (`chart_id`) REFERENCES `fat_snf_charts` (`id`) ON DELETE CASCADE",
        "SELECT COUNT(*) FROM fat_snf_chart_values v "
        "LEFT JOIN fat_snf_charts c ON c.id = v.chart_id WHERE c.id IS NULL"),
    _fk("fat_snf_milk_collection", "fk_fsn_entry_vendor",
        "FOREIGN KEY (`vendor_id`, `user_id`) REFERENCES `vendors` (`vendor_id`, `user_id`) ON DELETE CASCADE",
        "SELECT COUNT(*) FROM fat_snf_milk_collection m LEFT JOIN vendors v "
        "ON v.vendor_id = m.vendor_id AND v.user_id = m.user_id WHERE v.id IS NULL"),
    _fk("fat_snf_milk_collection", "fk_fsn_entry_chart",
        "FOREIGN KEY (`chart_id`) REFERENCES `fat_snf_charts` (`id`)",
        "SELECT COUNT(*) FROM fat_snf_milk_collection m "
        "LEFT JOIN fat_snf_charts c ON c.id = m.chart_id WHERE c.id IS NULL"),
    _fk("fat_snf_milk_collection", "fk_fsn_entry_chart_value",
        "FOREIGN KEY (`chart_value_id`) REFERENCES `fat_snf_chart_values` (`id`) ON DELETE SET NULL",
        "SELECT COUNT(*) FROM fat_snf_milk_collection m LEFT JOIN fat_snf_chart_values v "
        "ON v.id = m.chart_value_id WHERE m.chart_value_id IS NOT NULL AND v.id IS NULL"),
    _fk("fat_snf_milk_collection", "fk_fsn_entry_staff",
        "FOREIGN KEY (`staff_id`) REFERENCES `staff` (`id`) ON DELETE SET NULL",
        "SELECT COUNT(*) FROM fat_snf_milk_collection m LEFT JOIN staff s "
        "ON s.id = m.staff_id WHERE m.staff_id IS NOT NULL AND s.id IS NULL"),
]
