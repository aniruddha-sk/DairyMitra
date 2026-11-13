-- DairyMitr database schema (structure only)
-- Refined from the uploaded MySQL dump.
-- No INSERT statements or application data are included.
-- Review on a test database before running on production.

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- --------------------------------------------------------
-- Table: `users`
-- --------------------------------------------------------
CREATE TABLE `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `email` varchar(255) NOT NULL,
  `password` varchar(255) NOT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `dairy_name` varchar(255) DEFAULT NULL,
  `is_verified` tinyint(1) DEFAULT '0',
  `otp` varchar(10) DEFAULT NULL,
  `otp_expiry` datetime DEFAULT NULL,
  `reset_token` varchar(255) DEFAULT NULL,
  `reset_token_expiry` datetime DEFAULT NULL,
  `otp_code` varchar(6) DEFAULT NULL,
  `otp_created_at` datetime DEFAULT NULL,
  `security_password_hash` varchar(255) DEFAULT NULL,
  `security_otp` varchar(6) DEFAULT NULL,
  `security_otp_expiry` datetime DEFAULT NULL,
  `dairy_code` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `email` (`email`),
  UNIQUE KEY `dairy_code` (`dairy_code`)
) ENGINE=InnoDB AUTO_INCREMENT=11 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `vendors`
-- --------------------------------------------------------
CREATE TABLE `vendors` (
  `id` int NOT NULL AUTO_INCREMENT,
  `vendor_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `name_en` varchar(255) DEFAULT NULL,
  `address` text,
  `milk_type` enum('cow','buffalo','both') DEFAULT 'cow',
  `phone` varchar(15) DEFAULT NULL,
  `user_id` int NOT NULL,
  `ifsc_code` varchar(20) DEFAULT NULL,
  `account_no` varchar(30) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_vendor_per_user` (`vendor_id`,`user_id`),
  KEY `idx_vendor_user` (`user_id`),
  CONSTRAINT `vendors_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=213 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `staff`
-- --------------------------------------------------------
CREATE TABLE `staff` (
  `id` int NOT NULL AUTO_INCREMENT,
  `owner_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `email` varchar(255) DEFAULT NULL,
  `password` varchar(255) NOT NULL,
  `vehicle_number` varchar(20) DEFAULT NULL,
  `role` enum('driver','manager') DEFAULT 'driver',
  `is_active` tinyint(1) DEFAULT '1',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `email` (`email`),
  KEY `owner_id` (`owner_id`),
  CONSTRAINT `staff_ibfk_1` FOREIGN KEY (`owner_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=32 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `food_sack_rates`
-- --------------------------------------------------------
CREATE TABLE `food_sack_rates` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `rate` float NOT NULL,
  `date_from` date NOT NULL,
  `is_active` tinyint(1) DEFAULT '1',
  PRIMARY KEY (`id`),
  KEY `user_id` (`user_id`),
  CONSTRAINT `food_sack_rates_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=38 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `advance`
-- --------------------------------------------------------
CREATE TABLE `advance` (
  `id` int NOT NULL AUTO_INCREMENT,
  `vendor_id` int DEFAULT NULL,
  `user_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `amount` float NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_advance` (`vendor_id`,`user_id`,`date`),
  KEY `idx_advance_vendor_date` (`vendor_id`,`user_id`,`date`),
  CONSTRAINT `advance_ibfk_1` FOREIGN KEY (`vendor_id`, `user_id`) REFERENCES `vendors` (`vendor_id`, `user_id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=162 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `advance_backup_before_migration`
-- --------------------------------------------------------
CREATE TABLE `advance_backup_before_migration` (
  `id` int NOT NULL DEFAULT '0',
  `vendor_id` int DEFAULT NULL,
  `user_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `amount` float NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `advance_transactions`
-- --------------------------------------------------------
CREATE TABLE `advance_transactions` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `vendor_id` int NOT NULL,
  `user_id` int NOT NULL,
  `transaction_type` enum('advance','deduction') NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `transaction_date` date NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `legacy_advance_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_legacy_advance_id` (`legacy_advance_id`),
  KEY `idx_advance_vendor_date` (`vendor_id`,`user_id`,`transaction_date`),
  KEY `idx_advance_user_date` (`user_id`,`transaction_date`),
  KEY `idx_advance_vendor_type_date` (`vendor_id`,`user_id`,`transaction_type`,`transaction_date`),
  CONSTRAINT `fk_advance_vendor` FOREIGN KEY (`vendor_id`, `user_id`) REFERENCES `vendors` (`vendor_id`, `user_id`) ON DELETE CASCADE,
  CONSTRAINT `chk_advance_amount` CHECK ((`amount` > 0))
) ENGINE=InnoDB AUTO_INCREMENT=375 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `bank_settings`
-- --------------------------------------------------------
CREATE TABLE `bank_settings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `account_holder_name` varchar(150) NOT NULL,
  `bank_name` varchar(100) NOT NULL,
  `branch_name` varchar(100) DEFAULT NULL,
  `account_no` varchar(50) NOT NULL,
  `ifsc_code` varchar(20) NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `user_id` (`user_id`),
  CONSTRAINT `bank_settings_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `customer_notifications`
-- --------------------------------------------------------
CREATE TABLE `customer_notifications` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `vendor_id` int NOT NULL,
  `type` enum('milk','food_sack','advance') NOT NULL,
  `title` varchar(255) NOT NULL,
  `message` text NOT NULL,
  `is_read` tinyint(1) DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `user_id` (`user_id`),
  CONSTRAINT `customer_notifications_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=13903 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `dm_challenges`
-- --------------------------------------------------------
CREATE TABLE `dm_challenges` (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `dm_otp_cooldowns`
-- --------------------------------------------------------
CREATE TABLE `dm_otp_cooldowns` (
  `account_key` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `expires_at` bigint NOT NULL,
  PRIMARY KEY (`account_key`),
  KEY `ix_dm_cooldown_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `dm_outbox`
-- --------------------------------------------------------
CREATE TABLE `dm_outbox` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `kind` varchar(20) NOT NULL,
  `payload` mediumtext NOT NULL,
  `attempts` int NOT NULL DEFAULT '0',
  `available_at` bigint NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_dm_outbox_available` (`available_at`,`id`),
  KEY `ix_dm_outbox_tenant` (`user_id`),
  CONSTRAINT `fk_dm_outbox_owner` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `dm_rate_limits`
-- --------------------------------------------------------
CREATE TABLE `dm_rate_limits` (
  `bucket_key` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `hits` int NOT NULL,
  `expires_at` bigint NOT NULL,
  PRIMARY KEY (`bucket_key`),
  KEY `ix_dm_limit_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `dm_sessions`
-- --------------------------------------------------------
CREATE TABLE `dm_sessions` (
  `sid` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `payload` mediumtext NOT NULL,
  `version` bigint NOT NULL,
  `expires_at` bigint NOT NULL,
  PRIMARY KEY (`sid`),
  KEY `ix_dm_session_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `fat_snf_calculation_settings`
-- --------------------------------------------------------
CREATE TABLE `fat_snf_calculation_settings` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `calculation_mode` enum('general','fat_snf') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'general',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fat_snf_settings_user` (`user_id`),
  KEY `idx_fat_snf_settings_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------
-- Table: `fat_snf_chart_values`
-- --------------------------------------------------------
CREATE TABLE `fat_snf_chart_values` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `chart_id` int unsigned NOT NULL,
  `fat` decimal(5,2) NOT NULL,
  `snf` decimal(5,2) NOT NULL,
  `rate` decimal(10,2) NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fat_snf_chart_value` (`chart_id`,`fat`,`snf`),
  KEY `idx_fat_snf_values_chart` (`chart_id`),
  KEY `idx_fat_snf_values_lookup` (`chart_id`,`fat`,`snf`),
  KEY `idx_fat_snf_values_fat` (`chart_id`,`fat`),
  KEY `idx_fat_snf_values_snf` (`chart_id`,`snf`),
  CONSTRAINT `chk_fat_snf_values_fat` CHECK ((`fat` >= 0)),
  CONSTRAINT `chk_fat_snf_values_rate` CHECK ((`rate` >= 0)),
  CONSTRAINT `chk_fat_snf_values_snf` CHECK ((`snf` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------
-- Table: `fat_snf_charts`
-- --------------------------------------------------------
CREATE TABLE `fat_snf_charts` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `created_by_staff_id` int unsigned DEFAULT NULL,
  `chart_name` varchar(150) COLLATE utf8mb4_unicode_ci NOT NULL,
  `milk_type` enum('cow','buffalo','both') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'both',
  `effective_from` date NOT NULL,
  `status` enum('active','inactive') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'active',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_fat_snf_charts_user` (`user_id`),
  KEY `idx_fat_snf_charts_user_type` (`user_id`,`milk_type`),
  KEY `idx_fat_snf_charts_effective` (`user_id`,`milk_type`,`effective_from`),
  KEY `idx_fat_snf_charts_status` (`user_id`,`status`),
  KEY `idx_fat_snf_charts_lookup` (`user_id`,`milk_type`,`status`,`effective_from`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------
-- Table: `fat_snf_milk_collection`
-- --------------------------------------------------------
CREATE TABLE `fat_snf_milk_collection` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `vendor_id` int unsigned NOT NULL,
  `staff_id` int unsigned DEFAULT NULL,
  `chart_id` int unsigned NOT NULL,
  `chart_value_id` int unsigned DEFAULT NULL,
  `collection_date` date NOT NULL,
  `slot` enum('morning','evening') COLLATE utf8mb4_unicode_ci NOT NULL,
  `milk_type` enum('cow','buffalo') COLLATE utf8mb4_unicode_ci NOT NULL,
  `fat` decimal(5,2) NOT NULL,
  `snf` decimal(5,2) NOT NULL,
  `quantity` decimal(10,2) NOT NULL,
  `rate` decimal(10,2) NOT NULL,
  `amount` decimal(12,2) NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fat_snf_collection_entry` (`user_id`,`vendor_id`,`collection_date`,`slot`,`milk_type`),
  KEY `idx_fat_snf_collection_user_date` (`user_id`,`collection_date`),
  KEY `idx_fat_snf_collection_vendor_date` (`vendor_id`,`collection_date`),
  KEY `idx_fat_snf_collection_user_vendor_date` (`user_id`,`vendor_id`,`collection_date`),
  KEY `idx_fat_snf_collection_date_slot` (`user_id`,`collection_date`,`slot`),
  KEY `idx_fat_snf_collection_vendor_slot` (`user_id`,`vendor_id`,`collection_date`,`slot`),
  KEY `idx_fat_snf_collection_chart` (`chart_id`),
  KEY `idx_fat_snf_collection_chart_value` (`chart_value_id`),
  KEY `idx_fat_snf_collection_type` (`user_id`,`milk_type`,`collection_date`),
  CONSTRAINT `chk_fat_collection_amount` CHECK ((`amount` >= 0)),
  CONSTRAINT `chk_fat_collection_fat` CHECK ((`fat` >= 0)),
  CONSTRAINT `chk_fat_collection_quantity` CHECK ((`quantity` > 0)),
  CONSTRAINT `chk_fat_collection_rate` CHECK ((`rate` >= 0)),
  CONSTRAINT `chk_fat_collection_snf` CHECK ((`snf` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------------
-- Table: `food_sack`
-- --------------------------------------------------------
CREATE TABLE `food_sack` (
  `id` int NOT NULL AUTO_INCREMENT,
  `vendor_id` int DEFAULT NULL,
  `user_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `sack_qty` int NOT NULL,
  `sack_rate_id` int NOT NULL,
  `total_cost` float NOT NULL,
  `sack_rate` decimal(10,2) DEFAULT '0.00',
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_food` (`vendor_id`,`user_id`,`date`,`sack_rate_id`),
  KEY `sack_rate_id` (`sack_rate_id`),
  KEY `idx_food_vendor_date` (`vendor_id`,`user_id`,`date`),
  KEY `idx_food_user_vendor_date` (`user_id`,`vendor_id`,`date`),
  KEY `idx_food_user_date` (`user_id`,`date`),
  CONSTRAINT `food_sack_ibfk_1` FOREIGN KEY (`vendor_id`, `user_id`) REFERENCES `vendors` (`vendor_id`, `user_id`) ON DELETE CASCADE,
  CONSTRAINT `food_sack_ibfk_2` FOREIGN KEY (`sack_rate_id`) REFERENCES `food_sack_rates` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1338 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `milk_collection`
-- --------------------------------------------------------
CREATE TABLE `milk_collection` (
  `id` int NOT NULL AUTO_INCREMENT,
  `vendor_id` int DEFAULT NULL,
  `user_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `slot` enum('morning','evening') NOT NULL,
  `milk_type` enum('cow','buffalo') NOT NULL,
  `quantity` float NOT NULL,
  `staff_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_entry` (`vendor_id`,`user_id`,`date`,`slot`,`milk_type`),
  KEY `idx_milk_vendor_date` (`vendor_id`,`user_id`,`date`),
  KEY `fk_staff_milk` (`staff_id`),
  KEY `idx_milk_user_date` (`user_id`,`date`),
  CONSTRAINT `fk_staff_milk` FOREIGN KEY (`staff_id`) REFERENCES `staff` (`id`) ON DELETE SET NULL,
  CONSTRAINT `milk_collection_ibfk_1` FOREIGN KEY (`vendor_id`, `user_id`) REFERENCES `vendors` (`vendor_id`, `user_id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=49767 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `milk_rates`
-- --------------------------------------------------------
CREATE TABLE `milk_rates` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `animal` enum('cow','buffalo') NOT NULL,
  `rate` float NOT NULL,
  `date_from` date NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_rates_user` (`user_id`,`date_from`),
  CONSTRAINT `milk_rates_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=17 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `push_subscriptions`
-- --------------------------------------------------------
CREATE TABLE `push_subscriptions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `vendor_id` int NOT NULL,
  `endpoint` varchar(700) NOT NULL,
  `p256dh` text NOT NULL,
  `auth` text NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `endpoint` (`endpoint`),
  KEY `user_id` (`user_id`),
  CONSTRAINT `push_subscriptions_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=11 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------
-- Table: `vendor_milk_rates`
-- --------------------------------------------------------
CREATE TABLE `vendor_milk_rates` (
  `id` int NOT NULL AUTO_INCREMENT,
  `vendor_id` int NOT NULL,
  `user_id` int NOT NULL,
  `cow_rate` decimal(6,2) DEFAULT NULL,
  `buffalo_rate` decimal(6,2) DEFAULT NULL,
  `date_from` date NOT NULL,
  PRIMARY KEY (`id`),
  KEY `vendor_id` (`vendor_id`,`user_id`),
  KEY `idx_vendor_rates_user_date` (`user_id`,`date_from`),
  CONSTRAINT `vendor_milk_rates_ibfk_1` FOREIGN KEY (`vendor_id`, `user_id`) REFERENCES `vendors` (`vendor_id`, `user_id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=43 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

SET FOREIGN_KEY_CHECKS = 1;
