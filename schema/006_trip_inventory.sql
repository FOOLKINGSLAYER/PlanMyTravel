CREATE TABLE IF NOT EXISTS trip_inventory (
    trip_id TEXT PRIMARY KEY REFERENCES trips(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    provider TEXT,
    origin_code TEXT,
    destination_code TEXT,
    flight_status TEXT NOT NULL DEFAULT 'unavailable',
    hotel_status TEXT NOT NULL DEFAULT 'unavailable',
    flights_json TEXT NOT NULL DEFAULT '[]',
    hotels_json TEXT NOT NULL DEFAULT '[]',
    flight_notice TEXT,
    hotel_notice TEXT,
    notice TEXT,
    searched_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
