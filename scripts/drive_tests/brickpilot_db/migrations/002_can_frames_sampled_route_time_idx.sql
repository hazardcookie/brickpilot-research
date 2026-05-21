-- Keep manual labeler CAN timeline loads indexed as label-validation routes grow.
CREATE INDEX IF NOT EXISTS can_frames_sampled_route_time_idx ON can_frames_sampled(route_uuid, t_sec);
INSERT INTO schema_migrations(version) VALUES('002_can_frames_sampled_route_time_idx') ON CONFLICT(version) DO NOTHING;
