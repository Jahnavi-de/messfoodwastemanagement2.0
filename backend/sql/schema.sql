-- ---------------------------------------------------------------------------
-- Database tables for the hostel food-waste project.
--
-- The backend runs this file automatically when it starts (and
-- `python manage.py init-db` runs it by hand). Every statement is
-- "CREATE TABLE IF NOT EXISTS", so existing tables and data are never touched.
--
-- users / attendance / waste_predictions: same as the original XAMPP database.
-- iot_devices / sensor_readings: the IoT extension.
--
-- Works on MariaDB 10.2+ (XAMPP ships 10.4, Raspberry Pi OS ships newer) and MySQL 8+.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS `users` (
  `id`         INT(11)      NOT NULL AUTO_INCREMENT,
  `username`   VARCHAR(100) NOT NULL,
  `email`      VARCHAR(100) NOT NULL,
  `password`   VARCHAR(255) NOT NULL,
  `created_at` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `role`       ENUM('admin','student') NOT NULL DEFAULT 'admin',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `attendance` (
  `id`              INT(10) UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id`         INT(11) NOT NULL,
  `attendance_date` DATE    NOT NULL,
  `status`          ENUM('present') NOT NULL DEFAULT 'present',
  `marked_at`       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_student_attendance` (`user_id`, `attendance_date`),
  CONSTRAINT `attendance_user_fk` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `waste_predictions` (
  `id`                     INT(11)     NOT NULL AUTO_INCREMENT,
  `date`                   DATE        NOT NULL,
  `day_of_week`            VARCHAR(20) NOT NULL,
  `meal_type`              VARCHAR(20) NOT NULL,
  `students_enrolled`      INT(11)     NOT NULL,
  `average_attendance`     FLOAT       NOT NULL,
  `special_events`         VARCHAR(10) NOT NULL,
  `weather`                VARCHAR(20) NOT NULL,
  `holiday_period`         VARCHAR(10) NOT NULL,
  `number_of_menus`        INT(11)     NOT NULL,
  `leftover_previous_day`  FLOAT       NOT NULL,
  `predicted_waste`        FLOAT       NOT NULL,
  `created_at`             TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `cost`                   DECIMAL(10,2) DEFAULT NULL,
  `recommendation`         VARCHAR(20)   DEFAULT NULL,
  `confidence`             DECIMAL(5,2)  DEFAULT NULL,
  `menu_items`             TEXT          DEFAULT NULL,
  `recommended_rice_kg`    DECIMAL(10,2) DEFAULT NULL,
  `recommended_dal_kg`     DECIMAL(10,2) DEFAULT NULL,
  `recommended_roti_count` INT(11)       DEFAULT NULL,
  `recommended_veg_kg`     DECIMAL(10,2) DEFAULT NULL,
  `recommended_nonveg_kg`  DECIMAL(10,2) DEFAULT NULL,
  `current_waste_kg`       DECIMAL(10,2) DEFAULT NULL,
  `optimized_waste_kg`     DECIMAL(10,2) DEFAULT NULL,
  `waste_reduction_kg`     DECIMAL(10,2) DEFAULT NULL,
  `reduction_percent`      DECIMAL(5,2)  DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- One row per Raspberry Pi allowed to upload data.
-- The API key itself is never stored, only its SHA-256 hash.
CREATE TABLE IF NOT EXISTS `iot_devices` (
  `id`            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `device_id`     VARCHAR(64)  NOT NULL,
  `name`          VARCHAR(100) NOT NULL,
  `location`      VARCHAR(100) DEFAULT NULL,
  `api_key_hash`  CHAR(64)     NOT NULL,
  `is_active`     TINYINT(1)   NOT NULL DEFAULT 1,
  `last_seen_at`  DATETIME     DEFAULT NULL,
  `last_ip`       VARCHAR(45)  DEFAULT NULL,
  `created_at`    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_iot_device_id` (`device_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Raw sensor readings exactly as the Pi measured them.
--   sensor_name : the physical sensor on that Pi (waste_bin, rice_store, kitchen_air ...)
--   sensor_type : waste_weight | leftover_weight | raw_material_weight (kg),
--                 temperature (C) | humidity (%)
--   item        : for raw materials, which one (rice, dal, oil ...); '' otherwise
--   reading_uid : random id made on the Pi, so a retried upload is never stored twice
--   recorded_at : kitchen local time on the Pi
--   meal_date / meal_type : which meal the reading belongs to (from the Pi's meal_windows)
CREATE TABLE IF NOT EXISTS `sensor_readings` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `device_id`    VARCHAR(64)  NOT NULL,
  `reading_uid`  VARCHAR(64)  NOT NULL,
  `sensor_name`  VARCHAR(40)  NOT NULL,
  `sensor_type`  ENUM('waste_weight','leftover_weight','raw_material_weight','temperature','humidity') NOT NULL,
  `item`         VARCHAR(30)  NOT NULL DEFAULT '',
  `value`        DECIMAL(10,3) NOT NULL,
  `unit`         VARCHAR(8)   NOT NULL,
  `meal_date`    DATE         NOT NULL,
  `meal_type`    ENUM('breakfast','lunch','dinner','other') NOT NULL DEFAULT 'other',
  `recorded_at`  DATETIME     NOT NULL,
  `received_at`  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_reading_uid` (`device_id`, `reading_uid`),
  KEY `idx_meal` (`meal_date`, `meal_type`, `sensor_type`),
  KEY `idx_series` (`device_id`, `sensor_name`, `recorded_at`),
  KEY `idx_type_time` (`sensor_type`, `recorded_at`),
  CONSTRAINT `fk_reading_device` FOREIGN KEY (`device_id`)
    REFERENCES `iot_devices` (`device_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
