-- MariaDB 10.11 Configuration & Database Initialization for Minecraft Weihnachtsdorf 7
-- Host: pterodactyl-node1 (192.168.1.148)

CREATE DATABASE IF NOT EXISTS panel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS mc_luckperms CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS mc_coreprotect CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS mc_plan CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Users (Replace placeholder passwords before executing)
CREATE USER IF NOT EXISTS 'pterodactyl'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME_PTERO_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'pterodactyl'@'localhost' IDENTIFIED BY 'CHANGE_ME_PTERO_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'minecraft'@'192.168.1.%' IDENTIFIED BY 'CHANGE_ME_MC_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'minecraft'@'localhost' IDENTIFIED BY 'CHANGE_ME_MC_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'minecraft'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME_MC_DB_PASSWORD';

-- Grants
GRANT ALL PRIVILEGES ON panel.* TO 'pterodactyl'@'127.0.0.1' WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON panel.* TO 'pterodactyl'@'localhost' WITH GRANT OPTION;

GRANT ALL PRIVILEGES ON mc_luckperms.* TO 'minecraft'@'192.168.1.%';
GRANT ALL PRIVILEGES ON mc_coreprotect.* TO 'minecraft'@'192.168.1.%';
GRANT ALL PRIVILEGES ON mc_plan.* TO 'minecraft'@'192.168.1.%';

GRANT ALL PRIVILEGES ON mc_luckperms.* TO 'minecraft'@'localhost';
GRANT ALL PRIVILEGES ON mc_coreprotect.* TO 'minecraft'@'localhost';
GRANT ALL PRIVILEGES ON mc_plan.* TO 'minecraft'@'localhost';

GRANT ALL PRIVILEGES ON mc_luckperms.* TO 'minecraft'@'127.0.0.1';
GRANT ALL PRIVILEGES ON mc_coreprotect.* TO 'minecraft'@'127.0.0.1';
GRANT ALL PRIVILEGES ON mc_plan.* TO 'minecraft'@'127.0.0.1';

FLUSH PRIVILEGES;
