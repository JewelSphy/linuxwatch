CREATE TABLE IF NOT EXISTS server_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    cpu_usage REAL NOT NULL,
    ram_usage REAL NOT NULL,
    disk_usage REAL NOT NULL,
    swap_usage REAL NOT NULL,
    read_speed INTEGER NOT NULL,
    write_speed INTEGER NOT NULL,
    uptime REAL NOT NULL,
    network_in INTEGER NOT NULL,
    network_out INTEGER NOT NULL,
    net_in_speed REAL NOT NULL,
    net_out_speed REAL NOT NULL,
    load_1 REAL NOT NULL,
    load_5 REAL NOT NULL,
    load_15 REAL NOT NULL,
    cpu_temp REAL,
    open_connections INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cpu_cores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id INTEGER NOT NULL,
    core INTEGER NOT NULL,
    usage REAL NOT NULL,
    FOREIGN KEY (metric_id) REFERENCES server_metrics(id)
);

CREATE TABLE IF NOT EXISTS top_processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    cpu_percent REAL NOT NULL,
    memory_percent REAL NOT NULL,
    FOREIGN KEY (metric_id) REFERENCES server_metrics(id)
);
CREATE TABLE IF NOT EXISTS service_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id INTEGER NOT NULL,
    service TEXT NOT NULL,
    running INTEGER NOT NULL,
    FOREIGN KEY (metric_id) REFERENCES server_metrics(id)
);

CREATE TABLE IF NOT EXISTS ssh_failed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id INTEGER NOT NULL,
    count INTEGER NOT NULL,
    FOREIGN KEY (metric_id) REFERENCES server_metrics(id)
);


CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    level TEXT NOT NULL,
    value REAL NOT NULL,
    threshold REAL NOT NULL,
    fired_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME,
    resolved_value REAL
);
