CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    destination_id INTEGER REFERENCES destinations(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    name TEXT,
    slug TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'tour',
    short_description TEXT,
    description TEXT,
    duration_days INTEGER,
    duration_nights INTEGER,
    group_size_min INTEGER,
    group_size_max INTEGER,
    base_price REAL NOT NULL DEFAULT 0,
    price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'SGD',
    pricing_json TEXT NOT NULL DEFAULT '{}',
    itinerary_json TEXT NOT NULL DEFAULT '[]',
    inclusions_json TEXT NOT NULL DEFAULT '[]',
    exclusions_json TEXT NOT NULL DEFAULT '[]',
    highlights_json TEXT NOT NULL DEFAULT '[]',
    difficulty TEXT,
    rating REAL NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    cover_image_url TEXT,
    is_featured INTEGER NOT NULL DEFAULT 0,
    is_published INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'archived')),
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS package_departures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity INTEGER,
    seats_available INTEGER,
    price REAL,
    currency TEXT NOT NULL DEFAULT 'SGD',
    status TEXT NOT NULL DEFAULT 'available',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIEW IF NOT EXISTS departures AS
    SELECT id, package_id, starts_at, ends_at, capacity, seats_available, price,
           currency, status, created_at, updated_at
    FROM package_departures;

CREATE TABLE IF NOT EXISTS package_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    media_asset_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
    image_url TEXT NOT NULL,
    alt_text TEXT,
    caption TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (package_id, image_url)
);

CREATE TABLE IF NOT EXISTS package_itinerary_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    day_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    activities_json TEXT NOT NULL DEFAULT '[]',
    meals_json TEXT NOT NULL DEFAULT '[]',
    accommodation TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (package_id, day_number)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_reference TEXT UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
    package_id INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    departure_id INTEGER REFERENCES package_departures(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    travelers_count INTEGER NOT NULL DEFAULT 1,
    total_amount REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'SGD',
    contact_name TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    payment_status TEXT NOT NULL DEFAULT 'unpaid',
    booked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    balance REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'INR',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    transaction_type TEXT,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    currency TEXT NOT NULL DEFAULT 'INR',
    description TEXT,
    reference TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'SGD',
    category TEXT,
    spent_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_packages_destination_id ON packages(destination_id);
CREATE INDEX IF NOT EXISTS idx_packages_published_featured ON packages(is_published, is_featured);
CREATE UNIQUE INDEX IF NOT EXISTS idx_packages_slug_unique ON packages(slug);
CREATE INDEX IF NOT EXISTS idx_package_departures_dates ON package_departures(package_id, starts_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_package_images_unique ON package_images(package_id, image_url);
CREATE INDEX IF NOT EXISTS idx_package_itinerary_days_order ON package_itinerary_days(package_id, day_number);
CREATE INDEX IF NOT EXISTS idx_bookings_user_status ON bookings(user_id, status);
CREATE INDEX IF NOT EXISTS idx_bookings_trip_id ON bookings(trip_id);
CREATE INDEX IF NOT EXISTS idx_wallet_transactions_wallet_created ON wallet_transactions(wallet_id, created_at);
CREATE INDEX IF NOT EXISTS idx_expenses_user_trip ON expenses(user_id, trip_id);
