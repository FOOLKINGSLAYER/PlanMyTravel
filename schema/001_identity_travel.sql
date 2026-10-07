CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL DEFAULT '',
    phone TEXT,
    date_of_birth TEXT,
    country TEXT,
    preferred_currency TEXT NOT NULL DEFAULT 'USD',
    avatar_url TEXT,
    profile_image_url TEXT,
    bio TEXT,
    preferences_json TEXT NOT NULL DEFAULT '{}',
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    role TEXT NOT NULL DEFAULT 'traveler',
    is_active INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    email_verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS destinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    country TEXT,
    region TEXT,
    city TEXT,
    description TEXT,
    short_description TEXT,
    hero_image_url TEXT,
    image_url TEXT,
    latitude REAL,
    longitude REAL,
    is_published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    destination_id INTEGER REFERENCES destinations(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    start_date TEXT,
    end_date TEXT,
    travelers INTEGER NOT NULL DEFAULT 1,
    travelers_count INTEGER NOT NULL DEFAULT 1,
    traveler_count INTEGER NOT NULL DEFAULT 1,
    total_budget REAL,
    budget REAL,
    budget_amount REAL,
    currency TEXT NOT NULL DEFAULT 'USD',
    selected_tier TEXT,
    travel_style TEXT,
    style TEXT,
    origin TEXT,
    interests_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'planning',
    notes TEXT,
    preferences_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS itinerary_plans (
    id TEXT PRIMARY KEY,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    title TEXT,
    description TEXT,
    tier TEXT NOT NULL DEFAULT 'standard',
    summary TEXT,
    estimated_total REAL,
    json_payload TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    total_estimated_cost REAL,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS itinerary_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES itinerary_plans(id) ON DELETE CASCADE,
    day_number INTEGER NOT NULL,
    date TEXT,
    start_date TEXT,
    end_date TEXT,
    title TEXT,
    summary TEXT,
    notes TEXT,
    weather_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (plan_id, day_number)
);

CREATE TABLE IF NOT EXISTS itinerary_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_id INTEGER NOT NULL REFERENCES itinerary_days(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    location TEXT,
    start_time TEXT,
    end_time TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    item_type TEXT NOT NULL DEFAULT 'activity',
    category TEXT,
    duration_min INTEGER,
    estimated_cost REAL,
    lat REAL,
    lng REAL,
    route_from_prev_json TEXT,
    currency TEXT NOT NULL DEFAULT 'USD',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS guides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    bio TEXT,
    languages_json TEXT NOT NULL DEFAULT '[]',
    specialties_json TEXT NOT NULL DEFAULT '[]',
    rating REAL NOT NULL DEFAULT 0,
    is_verified INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS guide_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guide_id INTEGER NOT NULL REFERENCES guides(id) ON DELETE CASCADE,
    sender_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    recipient_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trips_user_id ON trips(user_id);
CREATE INDEX IF NOT EXISTS idx_trips_destination_id ON trips(destination_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_plans_trip_id ON itinerary_plans(trip_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_days_plan_id ON itinerary_days(plan_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_items_day_order ON itinerary_items(day_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_guide_messages_conversation ON guide_messages(guide_id, created_at);
