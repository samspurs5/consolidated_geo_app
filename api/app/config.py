from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    graphhopper_url: str = "http://graphhopper:8989"
    overpass_url: str = "http://overpass/api"

    osm_data_dir: Path = Path("/data/osm")
    osm_pbf_file: str = "region.pbf"

    # Empty → offline mode; set to a replication server for online updates
    replication_url: str = ""

    # Docker container / volume names (must match docker-compose.yml)
    overpass_container: str = "overpass"
    graphhopper_container: str = "graphhopper"
    overpass_db_volume: str = "consolidated_geo_app_overpass-db"
    # Bind-mount path for GraphHopper graph cache (inside the container)
    graphhopper_cache_dir: str = "/data/default-gh"

    class Config:
        env_file = ".env"

    @property
    def pbf_path(self) -> Path:
        return self.osm_data_dir / self.osm_pbf_file

    @property
    def state_path(self) -> Path:
        return self.osm_data_dir / "state.txt"

    @property
    def updates_dir(self) -> Path:
        return self.osm_data_dir / "updates"


settings = Settings()
