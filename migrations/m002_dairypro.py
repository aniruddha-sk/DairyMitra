"""
DairyPro: language preference, subscriptions (Razorpay), free trial,
offline-sync idempotency keys and a few lookup indexes.

Additive only, like 001: nothing is dropped, renamed or rewritten. New
columns are NULL-able (or have defaults) so the previous code keeps working
against the migrated schema.

Payment records are deliberately NOT deleted when a dairy account is
deleted (ON DELETE SET NULL): they are financial records. The trial ledger
is keyed by a hash of the e-mail address so deleting an account and signing
up again does not grant a second free trial.
"""
from migrations.runner import Step

VERSION = "002"
DESCRIPTION = "DairyPro: language, subscriptions/trial/payments, offline idempotency keys"


def _col(table, column, ddl):
    return Step(
        description=f"add {table}.{column}",
        sql=f"ALTER TABLE `{table}` ADD COLUMN {ddl}",
        applied=lambda s: s.column_exists(table, column),
    )


def _index(table, name, cols, unique=False):
    kind = "UNIQUE INDEX" if unique else "INDEX"
    return Step(
        description=f"index {table}.{name}",
        sql=f"ALTER TABLE `{table}` ADD {kind} `{name}` ({cols})",
        applied=lambda s: s.index_exists(table, name),
    )


def _table(name, ddl):
    return Step(
        description=f"create {name}",
        sql=ddl,
        applied=lambda s: s.table_exists(name),
    )


STEPS = [
    # ---------------------------------------------------------------
    # Language preference (mr / en / hi), per account type
    # ---------------------------------------------------------------
    _col("users", "preferred_language", "`preferred_language` varchar(5) DEFAULT NULL"),
    _col("staff", "preferred_language", "`preferred_language` varchar(5) DEFAULT NULL"),
    _col("vendors", "preferred_language", "`preferred_language` varchar(5) DEFAULT NULL"),

    # ---------------------------------------------------------------
    # Offline sync: idempotency keys for writes that are NOT naturally
    # idempotent (food sack is an upsert and needs none).
    # ---------------------------------------------------------------
    _col("milk_collection", "client_request_id",
         "`client_request_id` varchar(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL"),
    _index("milk_collection", "uq_milk_collection_request",
           "`user_id`,`client_request_id`", unique=True),
    _col("advance_transactions", "client_request_id",
         "`client_request_id` varchar(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL"),
    _index("advance_transactions", "uq_advance_request",
           "`user_id`,`client_request_id`", unique=True),

    # ---------------------------------------------------------------
    # Lookup indexes (customer notification badge, dashboard)
    # ---------------------------------------------------------------
    _index("customer_notifications", "ix_notif_vendor_read", "`user_id`,`vendor_id`,`is_read`"),

    # ---------------------------------------------------------------
    # Subscription plans (prices in paise; the server never trusts a
    # client-submitted amount or duration)
    # ---------------------------------------------------------------
    _table("dp_plans", """CREATE TABLE `dp_plans` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `name` varchar(60) NOT NULL,
  `duration_months` int NOT NULL,
  `price_paise` int NOT NULL,
  `savings_pct` int NOT NULL DEFAULT '0',
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `sort_order` int NOT NULL DEFAULT '0',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dp_plan_code` (`code`),
  CONSTRAINT `chk_dp_plan_price` CHECK ((`price_paise` > 0)),
  CONSTRAINT `chk_dp_plan_duration` CHECK ((`duration_months` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),
    Step(
        description="seed dp_plans (monthly 399, six_months 1199, yearly 1999)",
        sql="""INSERT IGNORE INTO `dp_plans`
  (`code`, `name`, `duration_months`, `price_paise`, `savings_pct`, `sort_order`) VALUES
  ('monthly', 'Monthly', 1, 39900, 0, 1),
  ('six_months', 'Six Months', 6, 119900, 33, 2),
  ('yearly', 'Yearly', 12, 199900, 44, 3)""",
        applied=lambda s: s.count("SELECT COUNT(*) FROM dp_plans") >= 3,
    ),

    # ---------------------------------------------------------------
    # Free trial: one per e-mail address (hash), server timestamps only
    # ---------------------------------------------------------------
    _table("dp_trials", """CREATE TABLE `dp_trials` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `email_hash` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `started_at` datetime NOT NULL,
  `ends_at` datetime NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dp_trial_email` (`email_hash`),
  UNIQUE KEY `uq_dp_trial_user` (`user_id`),
  CONSTRAINT `fk_dp_trial_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),

    # ---------------------------------------------------------------
    # Payment orders (one row per Razorpay order) and attempts
    # ---------------------------------------------------------------
    _table("dp_payment_orders", """CREATE TABLE `dp_payment_orders` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `plan_code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `duration_months` int NOT NULL,
  `amount_paise` int NOT NULL,
  `currency` char(3) NOT NULL DEFAULT 'INR',
  `receipt` varchar(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `razorpay_order_id` varchar(40) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  `razorpay_payment_id` varchar(40) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  `status` enum('created','attempted','pending','paid','failed','cancelled','refunded','partially_refunded')
      NOT NULL DEFAULT 'created',
  `verified_via` varchar(12) DEFAULT NULL,
  `failure_code` varchar(60) DEFAULT NULL,
  `failure_reason` varchar(255) DEFAULT NULL,
  `refunded_paise` int NOT NULL DEFAULT '0',
  `subscription_id` bigint DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `paid_at` datetime DEFAULT NULL,
  `last_checked_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dp_order_receipt` (`receipt`),
  UNIQUE KEY `uq_dp_order_rzp` (`razorpay_order_id`),
  UNIQUE KEY `uq_dp_order_payment` (`razorpay_payment_id`),
  KEY `ix_dp_order_user` (`user_id`,`created_at`),
  KEY `ix_dp_order_status` (`status`,`created_at`),
  CONSTRAINT `fk_dp_order_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `chk_dp_order_amount` CHECK ((`amount_paise` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),
    _table("dp_payment_attempts", """CREATE TABLE `dp_payment_attempts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `order_id` bigint NOT NULL,
  `event` varchar(30) NOT NULL,
  `razorpay_payment_id` varchar(40) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  `detail` varchar(500) DEFAULT NULL,
  `ip` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `ix_dp_attempt_order` (`order_id`,`created_at`),
  CONSTRAINT `fk_dp_attempt_order` FOREIGN KEY (`order_id`) REFERENCES `dp_payment_orders` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),

    # ---------------------------------------------------------------
    # Subscription periods (history: one row per paid period)
    # ---------------------------------------------------------------
    _table("dp_subscriptions", """CREATE TABLE `dp_subscriptions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `plan_code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `order_id` bigint NOT NULL,
  `status` enum('active','cancelled','refunded') NOT NULL DEFAULT 'active',
  `starts_at` datetime NOT NULL,
  `ends_at` datetime NOT NULL,
  `cancelled_at` datetime DEFAULT NULL,
  `cancel_reason` varchar(255) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dp_sub_order` (`order_id`),
  KEY `ix_dp_sub_user_end` (`user_id`,`status`,`ends_at`),
  CONSTRAINT `fk_dp_sub_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_dp_sub_order` FOREIGN KEY (`order_id`) REFERENCES `dp_payment_orders` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),

    # ---------------------------------------------------------------
    # Webhook events: event id is unique, so a replayed delivery is
    # recognised and ignored
    # ---------------------------------------------------------------
    _table("dp_webhook_events", """CREATE TABLE `dp_webhook_events` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `event_id` varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `event_type` varchar(60) NOT NULL,
  `razorpay_order_id` varchar(40) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  `razorpay_payment_id` varchar(40) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  `payload` mediumtext NOT NULL,
  `result` varchar(60) DEFAULT NULL,
  `received_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `processed_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dp_webhook_event` (`event_id`),
  KEY `ix_dp_webhook_order` (`razorpay_order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),

    # ---------------------------------------------------------------
    # Trial / renewal reminders already shown (no repeated popups,
    # across devices)
    # ---------------------------------------------------------------
    _table("dp_reminders", """CREATE TABLE `dp_reminders` (
  `user_id` int NOT NULL,
  `reminder_key` varchar(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `shown_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`,`reminder_key`),
  CONSTRAINT `fk_dp_reminder_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""),
]
