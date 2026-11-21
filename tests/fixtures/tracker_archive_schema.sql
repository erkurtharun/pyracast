-- Schema only (no rows) of the tables an evaluation can read in the kept pre-reset archive.

CREATE TABLE public.plants_plant (
    id bigint NOT NULL,
    name character varying(200) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    region_id bigint NOT NULL,
    current_configuration_id bigint,
    deleted_at timestamp with time zone,
    automatic_calibration_enabled boolean NOT NULL,
    automatic_calibration_enabled_by_id integer,
    next_calibration_check_at timestamp with time zone
);

CREATE TABLE public.plants_plantconfiguration (
    id bigint NOT NULL,
    revision integer NOT NULL,
    dc_capacity_mw double precision NOT NULL,
    inverter_capacity_mw double precision,
    export_limit_mw double precision,
    measurement_basis character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    binding_id bigint NOT NULL,
    created_by_id integer NOT NULL,
    plant_id bigint NOT NULL,
    mounting_type character varying(10) NOT NULL,
    surface_azimuth double precision,
    surface_tilt double precision,
    region_id bigint NOT NULL,
    CONSTRAINT plant_angles_require_fixed CHECK ((((mounting_type)::text = 'fixed'::text) OR ((surface_azimuth IS NULL) AND (surface_tilt IS NULL)))),
    CONSTRAINT plant_config_revision_positive CHECK ((revision >= 1)),
    CONSTRAINT plant_dc_capacity_finite CHECK (((dc_capacity_mw > (0.0)::double precision) AND (dc_capacity_mw < 'Infinity'::double precision))),
    CONSTRAINT plant_export_limit_finite CHECK (((export_limit_mw IS NULL) OR ((export_limit_mw > (0.0)::double precision) AND (export_limit_mw < 'Infinity'::double precision)))),
    CONSTRAINT plant_inverter_capacity_finite CHECK (((inverter_capacity_mw IS NULL) OR ((inverter_capacity_mw > (0.0)::double precision) AND (inverter_capacity_mw < 'Infinity'::double precision)))),
    CONSTRAINT plant_measurement_basis_valid CHECK (((measurement_basis)::text = ANY ((ARRAY['net_export'::character varying, 'ac_generation'::character varying])::text[]))),
    CONSTRAINT plant_mounting_type_supported CHECK (((mounting_type)::text = ANY ((ARRAY['unknown'::character varying, 'fixed'::character varying, 'tracking'::character varying])::text[]))),
    CONSTRAINT plant_surface_azimuth_valid CHECK (((surface_azimuth IS NULL) OR ((surface_azimuth >= (0.0)::double precision) AND (surface_azimuth < (360.0)::double precision)))),
    CONSTRAINT plant_surface_tilt_valid CHECK (((surface_tilt IS NULL) OR ((surface_tilt >= (0.0)::double precision) AND (surface_tilt <= (90.0)::double precision)))),
    CONSTRAINT plants_plantconfiguration_revision_check CHECK ((revision >= 0))
);

CREATE TABLE public.metering_epiassnapshot (
    id bigint NOT NULL,
    attempt smallint NOT NULL,
    target_day date NOT NULL,
    collected_at timestamp with time zone NOT NULL,
    available_at timestamp with time zone NOT NULL,
    raw_payload jsonb NOT NULL,
    readings jsonb NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    complete boolean NOT NULL,
    configuration_id bigint NOT NULL,
    connection_id bigint NOT NULL,
    job_id bigint NOT NULL,
    endpoint character varying(200) NOT NULL,
    normalization_policy character varying(40) NOT NULL,
    provider_plant jsonb NOT NULL,
    source_kind character varying(30) NOT NULL,
    CONSTRAINT epias_acquisition_attempt_positive CHECK ((attempt >= 1)),
    CONSTRAINT epias_acquisition_source_valid CHECK (((source_kind)::text = ANY ((ARRAY['realtime_generation'::character varying, 'settlement_injection'::character varying])::text[]))),
    CONSTRAINT metering_epiassnapshot_attempt_check CHECK ((attempt >= 0))
);

CREATE TABLE public.metering_epiasconnection (
    id bigint NOT NULL,
    provider_plant_id bigint NOT NULL,
    eic character varying(100) NOT NULL,
    name character varying(300) NOT NULL,
    enabled boolean NOT NULL,
    revision integer NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    connected_by_id integer NOT NULL,
    plant_id bigint NOT NULL,
    settlement_plant_id bigint,
    settlement_checked_at timestamp with time zone,
    CONSTRAINT epias_revision_positive CHECK ((revision >= 1)),
    CONSTRAINT metering_epiasconnection_provider_plant_id_check CHECK ((provider_plant_id >= 0)),
    CONSTRAINT metering_epiasconnection_revision_check CHECK ((revision >= 0)),
    CONSTRAINT metering_epiasconnection_settlement_plant_id_check CHECK ((settlement_plant_id >= 0))
);

CREATE TABLE public.forecasts_forecastresult (
    id bigint NOT NULL,
    payload jsonb NOT NULL,
    published_at timestamp with time zone NOT NULL,
    snapshot_id bigint NOT NULL,
    daily_payloads jsonb NOT NULL
);

CREATE TABLE public.forecasts_forecastsnapshot (
    id bigint NOT NULL,
    raw_payload jsonb NOT NULL,
    normalized_intervals jsonb NOT NULL,
    collected_at timestamp with time zone NOT NULL,
    issued_at timestamp with time zone,
    job_id bigint NOT NULL,
    source_kind character varying(20) NOT NULL,
    available_at timestamp with time zone,
    coverage_end timestamp with time zone,
    coverage_start timestamp with time zone,
    weather_error character varying(64) NOT NULL,
    weather_payload jsonb,
    binding_id bigint NOT NULL,
    CONSTRAINT snapshot_coverage_bounds CHECK ((((coverage_end IS NULL) AND (coverage_start IS NULL)) OR ((coverage_end > coverage_start) AND (coverage_end IS NOT NULL) AND (coverage_start IS NOT NULL)))),
    CONSTRAINT snapshot_source_kind CHECK (((source_kind)::text = ANY ((ARRAY['unknown'::character varying, 'forecast'::character varying, 'estimated_actuals'::character varying])::text[])))
);

CREATE TABLE public.plants_plantforecast (
    id bigint NOT NULL,
    resolution_minutes integer NOT NULL,
    target_date date NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    calibration_id bigint,
    configuration_id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    CONSTRAINT plant_forecast_resolution CHECK ((resolution_minutes = ANY (ARRAY[30, 60]))),
    CONSTRAINT plants_plantforecast_resolution_minutes_check CHECK ((resolution_minutes >= 0))
);

CREATE TABLE public.plants_forecastissuance (
    id bigint NOT NULL,
    target_date date NOT NULL,
    published_at timestamp with time zone NOT NULL,
    cutoff_at timestamp with time zone NOT NULL,
    model_version character varying(100) NOT NULL,
    factor double precision NOT NULL,
    payloads jsonb NOT NULL,
    outage_scenarios jsonb NOT NULL,
    native_intervals jsonb NOT NULL,
    calibration_id bigint,
    configuration_id bigint NOT NULL,
    plant_id bigint NOT NULL,
    snapshot_id bigint NOT NULL,
    CONSTRAINT forecast_issuance_before_daylight CHECK ((published_at < cutoff_at)),
    CONSTRAINT forecast_issuance_factor_finite CHECK (((factor > (0.0)::double precision) AND (factor < 'Infinity'::double precision))),
    CONSTRAINT issuance_baseline_factor_identity CHECK (((calibration_id IS NOT NULL) OR (factor = (1.0)::double precision)))
);

CREATE TABLE public.plants_calibrationrun (
    id bigint NOT NULL,
    window_start date NOT NULL,
    window_end date NOT NULL,
    cutoff_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    status character varying(20) NOT NULL,
    assessment character varying(200) NOT NULL,
    factor double precision,
    result jsonb NOT NULL,
    dataset_manifest jsonb NOT NULL,
    dataset_sha256 character varying(64) NOT NULL,
    lease_token uuid,
    lease_expires_at timestamp with time zone,
    attempts integer NOT NULL,
    last_error character varying(200) NOT NULL,
    binding_id bigint NOT NULL,
    requested_by_id integer NOT NULL,
    plant_id bigint NOT NULL,
    configuration_id bigint NOT NULL,
    observation_id_cutoff bigint,
    comparison_activation_id bigint,
    input_fingerprint character varying(64) NOT NULL,
    reference_snapshot_ids jsonb,
    production_snapshot_ids jsonb CONSTRAINT plants_calibrationrun_settlement_snapshot_ids_not_null NOT NULL,
    incident_revision_ids jsonb NOT NULL,
    measurement_selection_policy character varying(80) NOT NULL,
    CONSTRAINT calibration_factor_finite CHECK (((factor IS NULL) OR ((factor > (0.0)::double precision) AND (factor < 'Infinity'::double precision)))),
    CONSTRAINT calibration_status_valid CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'ready'::character varying, 'insufficient'::character varying, 'rejected'::character varying, 'failed'::character varying, 'superseded'::character varying])::text[]))),
    CONSTRAINT calibration_window_valid CHECK ((window_end >= window_start)),
    CONSTRAINT plants_calibrationrun_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT plants_calibrationrun_observation_id_cutoff_check CHECK ((observation_id_cutoff >= 0))
);

CREATE TABLE public.plants_calibrationactivation (
    id bigint NOT NULL,
    created_at timestamp with time zone NOT NULL,
    actor_id integer,
    calibration_id bigint,
    plant_id bigint NOT NULL,
    configuration_id bigint NOT NULL
);

CREATE TABLE public.plants_productionobservation (
    id bigint NOT NULL,
    starts_at timestamp with time zone NOT NULL,
    ends_at timestamp with time zone NOT NULL,
    energy_mwh double precision NOT NULL,
    quality character varying(20) NOT NULL,
    source character varying(200) NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    configuration_id bigint NOT NULL,
    plant_id bigint NOT NULL,
    recorded_by_id integer NOT NULL,
    is_current boolean NOT NULL,
    is_reverted boolean NOT NULL,
    supersedes_id bigint,
    CONSTRAINT plant_observation_duration CHECK (((ends_at = (starts_at + '00:30:00'::interval)) OR (ends_at = (starts_at + '01:00:00'::interval)))),
    CONSTRAINT plant_observation_energy_finite CHECK (((energy_mwh >= (0.0)::double precision) AND (energy_mwh < 'Infinity'::double precision))),
    CONSTRAINT plant_observation_quality_valid CHECK (((quality)::text = ANY ((ARRAY['valid'::character varying, 'outage'::character varying, 'curtailed'::character varying, 'invalid'::character varying])::text[]))),
    CONSTRAINT plant_observation_revert_has_source CHECK (((NOT is_reverted) OR (supersedes_id IS NOT NULL)))
);

CREATE TABLE public.connections_sitebinding (
    id bigint NOT NULL,
    region_revision integer NOT NULL,
    site_id character varying(200) NOT NULL,
    capacity_kw double precision NOT NULL,
    power_basis character varying(2) NOT NULL,
    capacity_semantics_verified boolean NOT NULL,
    tilt_degrees double precision NOT NULL,
    azimuth_degrees double precision NOT NULL,
    losses_percent double precision NOT NULL,
    installation_date date,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    timezone character varying(100) NOT NULL,
    validated_at timestamp with time zone,
    validated_credential_revision integer,
    health character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    provider_account_id bigint NOT NULL,
    region_id bigint NOT NULL,
    settings_verification_reference character varying(500) CONSTRAINT connections_sitebinding_settings_verification_referenc_not_null NOT NULL,
    dc_capacity_kw double precision,
    CONSTRAINT binding_azimuth CHECK (((azimuth_degrees >= (0.0)::double precision) AND (azimuth_degrees < (360.0)::double precision))),
    CONSTRAINT binding_capacity CHECK ((capacity_kw > (0.0)::double precision)),
    CONSTRAINT binding_capacity_finite CHECK ((capacity_kw < 'Infinity'::double precision)),
    CONSTRAINT binding_dc_capacity CHECK (((dc_capacity_kw IS NULL) OR ((dc_capacity_kw > (0.0)::double precision) AND (dc_capacity_kw < 'Infinity'::double precision)))),
    CONSTRAINT binding_health CHECK (((health)::text = ANY ((ARRAY['unknown'::character varying, 'healthy'::character varying, 'error'::character varying])::text[]))),
    CONSTRAINT binding_latitude CHECK (((latitude >= ('-90.0'::numeric)::double precision) AND (latitude <= (90.0)::double precision))),
    CONSTRAINT binding_longitude CHECK (((longitude >= ('-180.0'::numeric)::double precision) AND (longitude <= (180.0)::double precision))),
    CONSTRAINT binding_losses CHECK (((losses_percent >= (0.0)::double precision) AND (losses_percent < (100.0)::double precision))),
    CONSTRAINT binding_power_basis CHECK (((power_basis)::text = ANY ((ARRAY['ac'::character varying, 'dc'::character varying])::text[]))),
    CONSTRAINT connections_sitebinding_region_revision_check CHECK ((region_revision >= 0)),
    CONSTRAINT connections_sitebinding_validated_credential_revision_check CHECK ((validated_credential_revision >= 0)),
    CONSTRAINT horizontal_binding CHECK ((tilt_degrees = (0.0)::double precision))
);

CREATE TABLE public.regions_region (
    id bigint NOT NULL,
    name character varying(200) NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    timezone character varying(100) NOT NULL,
    revision integer NOT NULL,
    status character varying(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    approved_binding_id bigint,
    tenant_id bigint NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT region_latitude CHECK (((latitude >= ('-90.0'::numeric)::double precision) AND (latitude <= (90.0)::double precision))),
    CONSTRAINT region_longitude CHECK (((longitude >= ('-180.0'::numeric)::double precision) AND (longitude <= (180.0)::double precision))),
    CONSTRAINT region_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'suspended'::character varying])::text[]))),
    CONSTRAINT regions_region_revision_check CHECK ((revision >= 0))
);
