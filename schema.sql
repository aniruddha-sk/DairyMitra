CREATE DATABASE IF NOT EXISTS dairymitra;
USE dairymitra;

-- =========================================
-- USERS TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS users (

    id INT AUTO_INCREMENT PRIMARY KEY,

    email VARCHAR(255) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,

    phone VARCHAR(20),
    dairy_name VARCHAR(255),

    is_verified BOOLEAN DEFAULT FALSE,

    otp VARCHAR(10),
    otp_expiry DATETIME,

    reset_token VARCHAR(255),
    reset_token_expiry DATETIME,

    otp_code VARCHAR(6),
    otp_created_at DATETIME,

    security_password_hash VARCHAR(255),
    security_otp VARCHAR(6),
    security_otp_expiry DATETIME
);

-- =========================================
-- VENDORS TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS vendors (

    id INT AUTO_INCREMENT PRIMARY KEY,

    vendor_id INT NOT NULL,
    name VARCHAR(100) NOT NULL,

    address TEXT,

    milk_type ENUM('cow','buffalo','both') DEFAULT 'cow',

    phone VARCHAR(15),

    user_id INT NOT NULL,

    FOREIGN KEY (user_id)
    REFERENCES users(id)
    ON DELETE CASCADE,

    UNIQUE KEY unique_vendor_per_user (vendor_id,user_id)
);

-- =========================================
-- STAFF TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS staff (

    id INT AUTO_INCREMENT PRIMARY KEY,

    owner_id INT NOT NULL,

    name VARCHAR(100) NOT NULL,

    email VARCHAR(255) UNIQUE,

    password VARCHAR(255) NOT NULL,

    vehicle_number VARCHAR(20),

    role ENUM('driver','manager') DEFAULT 'driver',

    is_active BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (owner_id)
    REFERENCES users(id)
    ON DELETE CASCADE
);

-- =========================================
-- MILK COLLECTION TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS milk_collection (

    id INT AUTO_INCREMENT PRIMARY KEY,

    vendor_id INT,
    user_id INT,
    staff_id INT,

    date DATE,

    slot ENUM('morning','evening') NOT NULL,

    milk_type ENUM('cow','buffalo') NOT NULL,

    quantity FLOAT NOT NULL,

    FOREIGN KEY (vendor_id,user_id)
    REFERENCES vendors(vendor_id,user_id)
    ON DELETE CASCADE,

    FOREIGN KEY (staff_id)
    REFERENCES staff(id)
    ON DELETE SET NULL,

    UNIQUE KEY unique_entry
    (vendor_id,user_id,date,slot,milk_type)
);

-- =========================================
-- ADVANCE TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS advance (

    id INT AUTO_INCREMENT PRIMARY KEY,

    vendor_id INT,
    user_id INT,

    date DATE,

    amount FLOAT NOT NULL,

    FOREIGN KEY (vendor_id,user_id)
    REFERENCES vendors(vendor_id,user_id)
    ON DELETE CASCADE,

    UNIQUE KEY unique_advance
    (vendor_id,user_id,date)
);

-- =========================================
-- FOOD SACK RATE TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS food_sack_rates (

    id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL,

    name VARCHAR(100) NOT NULL,

    rate FLOAT NOT NULL,

    date_from DATE NOT NULL,

    is_active TINYINT(1) DEFAULT 1,

    FOREIGN KEY (user_id)
    REFERENCES users(id)
    ON DELETE CASCADE
);

-- =========================================
-- FOOD SACK TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS food_sack (

    id INT AUTO_INCREMENT PRIMARY KEY,

    vendor_id INT,
    user_id INT,

    date DATE,

    sack_qty INT NOT NULL,

    sack_rate_id INT NOT NULL,

    sack_rate DECIMAL(10,2),

    total_cost FLOAT NOT NULL,

    FOREIGN KEY (vendor_id,user_id)
    REFERENCES vendors(vendor_id,user_id)
    ON DELETE CASCADE,

    FOREIGN KEY (sack_rate_id)
    REFERENCES food_sack_rates(id)
    ON DELETE CASCADE,

    UNIQUE KEY unique_food
    (vendor_id,user_id,date,sack_rate_id)
);

-- =========================================
-- MILK RATE TABLE
-- =========================================
CREATE TABLE IF NOT EXISTS milk_rates (

    id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL,

    animal ENUM('cow','buffalo') NOT NULL,

    rate FLOAT NOT NULL,

    date_from DATE NOT NULL,

    FOREIGN KEY (user_id)
    REFERENCES users(id)
    ON DELETE CASCADE
);

-- =========================================
-- VENDOR SPECIAL RATE
-- =========================================
CREATE TABLE IF NOT EXISTS vendor_milk_rates (

    id INT AUTO_INCREMENT PRIMARY KEY,

    vendor_id INT NOT NULL,
    user_id INT NOT NULL,

    cow_rate DECIMAL(6,2),
    buffalo_rate DECIMAL(6,2),

    date_from DATE NOT NULL,

    FOREIGN KEY (vendor_id,user_id)
    REFERENCES vendors(vendor_id,user_id)
    ON DELETE CASCADE
);

-- =========================================
-- INDEXES (PERFORMANCE)
-- =========================================

CREATE INDEX idx_milk_user_vendor_date
ON milk_collection(user_id,vendor_id,date);

CREATE INDEX idx_milk_user_date
ON milk_collection(user_id,date);

CREATE INDEX idx_advance_user_date
ON advance(user_id,date);

CREATE INDEX idx_food_user_date
ON food_sack(user_id,date);

CREATE INDEX idx_food_user_vendor_date
ON food_sack(user_id,vendor_id,date);

CREATE INDEX idx_sack_rate_user
ON food_sack_rates(user_id,is_active);

CREATE INDEX idx_vendor_user
ON vendors(user_id);

CREATE INDEX idx_rates_user
ON milk_rates(user_id,date_from);


CREATE INDEX idx_vendor_rates_user_date
ON vendor_milk_rates(user_id, date_from);



ALTER TABLE vendors
ADD COLUMN ifsc_code VARCHAR(20),
ADD COLUMN account_no VARCHAR(30);
DESCRIBE vendors;


CREATE TABLE bank_settings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL UNIQUE,

    account_holder_name VARCHAR(150) NOT NULL,
    bank_name VARCHAR(100) NOT NULL,
    branch_name VARCHAR(100),

    account_no VARCHAR(50) NOT NULL,
    ifsc_code VARCHAR(20) NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);



-- =========================================
-- CREATE DATABASE
-- =========================================

CREATE DATABASE IF NOT EXISTS dairymitra;
USE dairymitra;

-- =========================================
-- USERS TABLE UPDATE
-- =========================================

ALTER TABLE users
ADD COLUMN dairy_code VARCHAR(20) UNIQUE;

-- =========================================
-- VENDORS TABLE UPDATE
-- =========================================

ALTER TABLE vendors
ADD COLUMN name_en VARCHAR(255) NULL AFTER name,
ADD COLUMN ifsc_code VARCHAR(20),
ADD COLUMN account_no VARCHAR(30);

-- =========================================
-- BANK SETTINGS
-- =========================================

CREATE TABLE IF NOT EXISTS bank_settings (

    id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL UNIQUE,

    account_holder_name VARCHAR(150) NOT NULL,
    bank_name VARCHAR(100) NOT NULL,
    branch_name VARCHAR(100),

    account_no VARCHAR(50) NOT NULL,
    ifsc_code VARCHAR(20) NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);

-- =========================================
-- CUSTOMER NOTIFICATIONS
-- =========================================

CREATE TABLE IF NOT EXISTS customer_notifications (

    id INT AUTO_INCREMENT PRIMARY KEY,

    -- Dairy Owner ID
    user_id INT NOT NULL,

    -- Vendor ID
    vendor_id INT NOT NULL,

    -- Notification Type
    type ENUM(
        'milk',
        'food_sack',
        'advance'
    ) NOT NULL,

    -- Notification Title
    title VARCHAR(255) NOT NULL,

    -- Notification Message
    message TEXT NOT NULL,

    -- Read Status
    is_read BOOLEAN NOT NULL DEFAULT FALSE,

    -- Created Time
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);

-- =========================================
-- CUSTOMER NOTIFICATION INDEXES
-- =========================================

CREATE INDEX idx_notification_vendor
ON customer_notifications(vendor_id);

CREATE INDEX idx_notification_user
ON customer_notifications(user_id);

CREATE INDEX idx_notification_read
ON customer_notifications(is_read);

CREATE INDEX idx_notification_created
ON customer_notifications(created_at);

CREATE INDEX idx_notification_lookup
ON customer_notifications(user_id, vendor_id, created_at);

-- =========================================
-- PUSH SUBSCRIPTIONS
-- =========================================

CREATE TABLE IF NOT EXISTS push_subscriptions (

    id INT AUTO_INCREMENT PRIMARY KEY,

    -- Dairy Owner ID
    user_id INT NOT NULL,

    -- Vendor ID
    vendor_id INT NOT NULL,

    -- Browser Push Endpoint
    endpoint VARCHAR(700) NOT NULL UNIQUE,

    -- Public Encryption Key
    p256dh TEXT NOT NULL,

    -- Authentication Secret
    auth TEXT NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);

-- =========================================
-- PUSH SUBSCRIPTION INDEX
-- =========================================

CREATE INDEX idx_push_lookup
ON push_subscriptions(user_id, vendor_id);

-- =========================================
-- VENDOR MILK RATE INDEX
-- =========================================

CREATE INDEX idx_vendor_rates_user_date
ON vendor_milk_rates(user_id, date_from);

-- =========================================
-- SAMPLE QUERY
-- =========================================

SELECT
    v.vendor_id,
    v.name
FROM vendors v
JOIN users u
    ON v.user_id = u.id
WHERE u.email = 'jagadalerushi99@gmail.com'
ORDER BY v.vendor_id;

-- =========================================
-- VERIFY TABLES
-- =========================================

DESCRIBE users;
DESCRIBE vendors;


CREATE TABLE IF NOT EXISTS advance_transactions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    vendor_id INT NOT NULL,
    user_id INT NOT NULL,

    transaction_type ENUM('advance', 'deduction') NOT NULL,

    amount DECIMAL(12,2) NOT NULL,

    transaction_date DATE NOT NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (id),

    -- Fast vendor-wise ledger queries
    INDEX idx_advance_vendor_date (
        vendor_id,
        user_id,
        transaction_date
    ),

    -- Fast user-wise date-range queries
    INDEX idx_advance_user_date (
        user_id,
        transaction_date
    ),

    -- Useful for transaction-type filtering
    INDEX idx_advance_vendor_type_date (
        vendor_id,
        user_id,
        transaction_type,
        transaction_date
    ),

    CONSTRAINT fk_advance_vendor
        FOREIGN KEY (vendor_id, user_id)
        REFERENCES vendors(vendor_id, user_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_advance_amount
        CHECK (amount > 0)

) ENGINE=InnoDB;