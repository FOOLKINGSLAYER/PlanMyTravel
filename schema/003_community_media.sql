CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    folder TEXT,
    relative_path TEXT NOT NULL UNIQUE,
    path TEXT UNIQUE,
    url TEXT,
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT,
    size_bytes INTEGER,
    file_size INTEGER,
    width INTEGER,
    height INTEGER,
    alt_text TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    destination_id INTEGER REFERENCES destinations(id) ON DELETE CASCADE,
    package_id INTEGER REFERENCES packages(id) ON DELETE CASCADE,
    trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    title TEXT,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'published',
    is_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, trip_id)
);

CREATE TABLE IF NOT EXISTS review_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    media_asset_id INTEGER REFERENCES media_assets(id) ON DELETE SET NULL,
    image_url TEXT NOT NULL,
    caption TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS saved_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, item_type, item_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_destination ON reviews(destination_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reviews_package ON reviews(package_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_photos_review_order ON review_photos(review_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_saved_items_user_created ON saved_items(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_media_assets_owner ON media_assets(owner_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_assets_relative_path ON media_assets(relative_path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_user_trip_unique ON reviews(user_id, trip_id);
