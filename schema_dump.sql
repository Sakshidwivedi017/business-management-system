--
-- PostgreSQL database dump
--

\restrict BpwNxURIBgchPf5VATqrwyQSbcLL1Rxt14sTj8g4V044Necl7efdMO6C72oIQUf

-- Dumped from database version 18.6 (Debian 18.6-1.pgdg13+2)
-- Dumped by pg_dump version 18.4 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: postgres
--

-- *not* creating schema, since initdb creates it


ALTER SCHEMA public OWNER TO postgres;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: postgres
--

COMMENT ON SCHEMA public IS '';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_audit_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.agent_audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    operation_id character varying(64) NOT NULL,
    user_id uuid NOT NULL,
    role character varying(32) NOT NULL,
    operation character varying(64) NOT NULL,
    entity_type character varying(64) NOT NULL,
    entity_id character varying(64),
    before_state jsonb,
    after_state jsonb,
    status character varying(16) NOT NULL,
    error text,
    thread_id character varying(128),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT ck_audit_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'success'::character varying, 'failed'::character varying])::text[])))
);


ALTER TABLE public.agent_audit_log OWNER TO postgres;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO postgres;

--
-- Name: checkpoint_blobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);


ALTER TABLE public.checkpoint_blobs OWNER TO postgres;

--
-- Name: checkpoint_migrations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.checkpoint_migrations (
    v integer NOT NULL
);


ALTER TABLE public.checkpoint_migrations OWNER TO postgres;

--
-- Name: checkpoint_writes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);


ALTER TABLE public.checkpoint_writes OWNER TO postgres;

--
-- Name: checkpoints; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.checkpoints OWNER TO postgres;

--
-- Name: inv_categories; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_categories (
    id character varying NOT NULL,
    name character varying NOT NULL,
    short_code character varying NOT NULL,
    "position" integer NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.inv_categories OWNER TO postgres;

--
-- Name: inv_current_stock; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_current_stock (
    id character varying NOT NULL,
    item_id character varying NOT NULL,
    location_id character varying NOT NULL,
    quantity double precision NOT NULL,
    min_stock_level double precision,
    max_stock_level double precision,
    last_transaction_at timestamp without time zone,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.inv_current_stock OWNER TO postgres;

--
-- Name: inv_field_definitions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_field_definitions (
    id character varying NOT NULL,
    name character varying NOT NULL,
    field_type character varying NOT NULL,
    required boolean NOT NULL,
    options text,
    "position" integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.inv_field_definitions OWNER TO postgres;

--
-- Name: inv_issued_to_targets; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_issued_to_targets (
    id character varying NOT NULL,
    name character varying NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.inv_issued_to_targets OWNER TO postgres;

--
-- Name: inv_items; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_items (
    id character varying NOT NULL,
    item_code character varying NOT NULL,
    name character varying NOT NULL,
    category_id character varying,
    sub_category_id character varying,
    sub_category_2_id character varying,
    status character varying NOT NULL,
    unit character varying NOT NULL,
    hsn_code character varying,
    notes text,
    specifications text,
    alt_units text,
    is_spare boolean DEFAULT false NOT NULL
);


ALTER TABLE public.inv_items OWNER TO postgres;

--
-- Name: inv_locations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_locations (
    id character varying NOT NULL,
    name character varying NOT NULL,
    address text,
    is_active boolean NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.inv_locations OWNER TO postgres;

--
-- Name: inv_sub_categories; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_sub_categories (
    id character varying NOT NULL,
    name character varying NOT NULL,
    short_code character varying NOT NULL,
    category_id character varying NOT NULL,
    "position" integer NOT NULL,
    created_at timestamp without time zone NOT NULL
);


ALTER TABLE public.inv_sub_categories OWNER TO postgres;

--
-- Name: inv_sub_categories_2; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_sub_categories_2 (
    id character varying NOT NULL,
    name character varying NOT NULL,
    short_code character varying NOT NULL,
    sub_category_id character varying NOT NULL,
    source_id character varying,
    standard text,
    description text,
    naming_convention text,
    convention_notes text,
    examples text,
    "position" integer NOT NULL,
    created_at timestamp without time zone NOT NULL,
    hsn_code character varying,
    nomenclature_fields text,
    nomenclature_version integer DEFAULT 1 NOT NULL,
    cut_shape character varying,
    cut_formula text
);


ALTER TABLE public.inv_sub_categories_2 OWNER TO postgres;

--
-- Name: inv_transactions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.inv_transactions (
    id character varying NOT NULL,
    item_id character varying NOT NULL,
    location_id character varying,
    transaction_type character varying NOT NULL,
    quantity double precision NOT NULL,
    reference_type character varying,
    reference_id character varying,
    transfer_location_id character varying,
    unit_cost double precision,
    notes text,
    created_by character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    currency character varying(3) DEFAULT 'INR'::character varying NOT NULL,
    issued_to_id character varying
);


ALTER TABLE public.inv_transactions OWNER TO postgres;

--
-- Name: po_template_lines; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.po_template_lines (
    id uuid NOT NULL,
    template_id uuid NOT NULL,
    inv_item_id uuid NOT NULL,
    quantity numeric(18,4) NOT NULL,
    unit text NOT NULL,
    unit_price numeric(18,4) NOT NULL,
    tax_percentage numeric(5,2) DEFAULT 18.0 NOT NULL,
    notes text,
    "position" integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.po_template_lines OWNER TO postgres;

--
-- Name: po_templates; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.po_templates (
    id uuid NOT NULL,
    name text NOT NULL,
    vendor_id uuid NOT NULL,
    currency text DEFAULT 'INR'::text NOT NULL,
    delivery_location_id uuid,
    payment_terms text,
    ship_to_address text,
    notes text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.po_templates OWNER TO postgres;

--
-- Name: proc_po_line_regularisations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_po_line_regularisations (
    id character varying NOT NULL,
    po_id character varying NOT NULL,
    po_line_id character varying NOT NULL,
    ordered_qty numeric(14,3) NOT NULL,
    received_qty_before numeric(14,3) NOT NULL,
    regularised_qty numeric(14,3) NOT NULL,
    received_qty_after numeric(14,3) NOT NULL,
    qty_variance numeric(14,3) NOT NULL,
    unit_price numeric(14,2) NOT NULL,
    actual_amount numeric(14,2) NOT NULL,
    expected_amount numeric(14,2) NOT NULL,
    amount_variance numeric(14,2) NOT NULL,
    notes text,
    inv_transaction_id character varying,
    regularised_by character varying NOT NULL,
    regularised_at timestamp without time zone NOT NULL,
    reviewed_by character varying,
    reviewed_at timestamp without time zone,
    review_notes text
);


ALTER TABLE public.proc_po_line_regularisations OWNER TO postgres;

--
-- Name: proc_po_lines; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_po_lines (
    id character varying NOT NULL,
    po_id character varying NOT NULL,
    inv_item_id character varying,
    quantity_ordered numeric(14,3) NOT NULL,
    quantity_received numeric(14,3) NOT NULL,
    unit character varying,
    unit_price numeric(14,2) NOT NULL,
    tax_percentage numeric(5,2) NOT NULL,
    line_subtotal numeric(14,2) NOT NULL,
    line_tax numeric(14,2) NOT NULL,
    line_total numeric(14,2) NOT NULL,
    notes text,
    "position" integer NOT NULL,
    custom_fields text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    is_regularised boolean DEFAULT false NOT NULL,
    expected_delivery_date json,
    expected_dispatch_date json
);


ALTER TABLE public.proc_po_lines OWNER TO postgres;

--
-- Name: proc_po_payment_tranches; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_po_payment_tranches (
    id character varying NOT NULL,
    po_id character varying NOT NULL,
    sequence integer NOT NULL,
    stage character varying NOT NULL,
    percentage numeric(5,2) NOT NULL,
    amount numeric(14,2),
    is_advance boolean NOT NULL,
    blocks_dispatch boolean NOT NULL,
    due_basis character varying,
    due_offset_days integer,
    source character varying NOT NULL,
    cleared_at timestamp without time zone,
    cleared_by character varying,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.proc_po_payment_tranches OWNER TO postgres;

--
-- Name: proc_po_receipts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_po_receipts (
    id character varying NOT NULL,
    po_line_id character varying NOT NULL,
    quantity numeric(14,3) NOT NULL,
    location_id character varying,
    notes text,
    inv_transaction_id character varying,
    received_by character varying NOT NULL,
    received_at timestamp without time zone NOT NULL
);


ALTER TABLE public.proc_po_receipts OWNER TO postgres;

--
-- Name: proc_purchase_orders; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_purchase_orders (
    id character varying NOT NULL,
    po_number character varying NOT NULL,
    vendor_id character varying NOT NULL,
    status character varying NOT NULL,
    placed_on date,
    currency character varying NOT NULL,
    subtotal numeric(14,2) NOT NULL,
    tax_amount numeric(14,2) NOT NULL,
    total_amount numeric(14,2) NOT NULL,
    payment_terms character varying,
    delivery_location_id character varying,
    ship_to_address text,
    custom_fields text,
    created_by character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    expected_delivery_date json,
    expected_dispatch_date json
);


ALTER TABLE public.proc_purchase_orders OWNER TO postgres;

--
-- Name: proc_unexpected_receipt_headers; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_unexpected_receipt_headers (
    id character varying NOT NULL,
    received_by character varying NOT NULL,
    received_at timestamp without time zone NOT NULL,
    notes text,
    vendor_name character varying
);


ALTER TABLE public.proc_unexpected_receipt_headers OWNER TO postgres;

--
-- Name: proc_unexpected_receipts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_unexpected_receipts (
    id character varying NOT NULL,
    inv_item_id character varying NOT NULL,
    quantity numeric(14,3) NOT NULL,
    location_id character varying,
    notes text,
    inv_transaction_id character varying,
    received_by character varying NOT NULL,
    received_at timestamp without time zone NOT NULL,
    resolved_po_id character varying,
    resolved_by character varying,
    resolved_at timestamp without time zone,
    header_id text
);


ALTER TABLE public.proc_unexpected_receipts OWNER TO postgres;

--
-- Name: proc_vendors; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.proc_vendors (
    id character varying NOT NULL,
    name character varying NOT NULL,
    code character varying,
    city character varying,
    payment_terms character varying,
    is_active boolean NOT NULL,
    created_by character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


ALTER TABLE public.proc_vendors OWNER TO postgres;

--
-- Name: refresh_tokens; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.refresh_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.refresh_tokens OWNER TO postgres;

--
-- Name: user_alert_emails; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.user_alert_emails (
    user_id text NOT NULL,
    email text NOT NULL,
    role text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.user_alert_emails OWNER TO postgres;

--
-- Name: users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying NOT NULL,
    password_hash character varying NOT NULL,
    full_name character varying NOT NULL,
    role character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    token_version integer DEFAULT 0 NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_users_role CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, 'procurement_manager'::character varying, 'inventory_manager'::character varying])::text[])))
);


ALTER TABLE public.users OWNER TO postgres;

--
-- Name: agent_audit_log agent_audit_log_operation_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.agent_audit_log
    ADD CONSTRAINT agent_audit_log_operation_id_key UNIQUE (operation_id);


--
-- Name: agent_audit_log agent_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.agent_audit_log
    ADD CONSTRAINT agent_audit_log_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: checkpoint_blobs checkpoint_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);


--
-- Name: checkpoint_migrations checkpoint_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);


--
-- Name: checkpoint_writes checkpoint_writes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);


--
-- Name: checkpoints checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);


--
-- Name: inv_categories inv_categories_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_categories
    ADD CONSTRAINT inv_categories_name_key UNIQUE (name);


--
-- Name: inv_categories inv_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_categories
    ADD CONSTRAINT inv_categories_pkey PRIMARY KEY (id);


--
-- Name: inv_categories inv_categories_short_code_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_categories
    ADD CONSTRAINT inv_categories_short_code_key UNIQUE (short_code);


--
-- Name: inv_current_stock inv_current_stock_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_current_stock
    ADD CONSTRAINT inv_current_stock_pkey PRIMARY KEY (id);


--
-- Name: inv_field_definitions inv_field_definitions_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_field_definitions
    ADD CONSTRAINT inv_field_definitions_name_key UNIQUE (name);


--
-- Name: inv_field_definitions inv_field_definitions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_field_definitions
    ADD CONSTRAINT inv_field_definitions_pkey PRIMARY KEY (id);


--
-- Name: inv_issued_to_targets inv_issued_to_targets_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_issued_to_targets
    ADD CONSTRAINT inv_issued_to_targets_name_key UNIQUE (name);


--
-- Name: inv_issued_to_targets inv_issued_to_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_issued_to_targets
    ADD CONSTRAINT inv_issued_to_targets_pkey PRIMARY KEY (id);


--
-- Name: inv_items inv_items_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_items
    ADD CONSTRAINT inv_items_pkey PRIMARY KEY (id);


--
-- Name: inv_locations inv_locations_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_locations
    ADD CONSTRAINT inv_locations_name_key UNIQUE (name);


--
-- Name: inv_locations inv_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_locations
    ADD CONSTRAINT inv_locations_pkey PRIMARY KEY (id);


--
-- Name: inv_sub_categories_2 inv_sub_categories_2_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories_2
    ADD CONSTRAINT inv_sub_categories_2_pkey PRIMARY KEY (id);


--
-- Name: inv_sub_categories inv_sub_categories_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories
    ADD CONSTRAINT inv_sub_categories_pkey PRIMARY KEY (id);


--
-- Name: inv_transactions inv_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_transactions
    ADD CONSTRAINT inv_transactions_pkey PRIMARY KEY (id);


--
-- Name: refresh_tokens pk_refresh_tokens; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT pk_refresh_tokens PRIMARY KEY (id);


--
-- Name: users pk_users; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);


--
-- Name: po_template_lines po_template_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.po_template_lines
    ADD CONSTRAINT po_template_lines_pkey PRIMARY KEY (id);


--
-- Name: po_templates po_templates_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.po_templates
    ADD CONSTRAINT po_templates_name_key UNIQUE (name);


--
-- Name: po_templates po_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.po_templates
    ADD CONSTRAINT po_templates_pkey PRIMARY KEY (id);


--
-- Name: proc_po_line_regularisations proc_po_line_regularisations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_line_regularisations
    ADD CONSTRAINT proc_po_line_regularisations_pkey PRIMARY KEY (id);


--
-- Name: proc_po_lines proc_po_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_lines
    ADD CONSTRAINT proc_po_lines_pkey PRIMARY KEY (id);


--
-- Name: proc_po_payment_tranches proc_po_payment_tranches_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_payment_tranches
    ADD CONSTRAINT proc_po_payment_tranches_pkey PRIMARY KEY (id);


--
-- Name: proc_po_receipts proc_po_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_receipts
    ADD CONSTRAINT proc_po_receipts_pkey PRIMARY KEY (id);


--
-- Name: proc_purchase_orders proc_purchase_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_purchase_orders
    ADD CONSTRAINT proc_purchase_orders_pkey PRIMARY KEY (id);


--
-- Name: proc_unexpected_receipt_headers proc_unexpected_receipt_headers_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_unexpected_receipt_headers
    ADD CONSTRAINT proc_unexpected_receipt_headers_pkey PRIMARY KEY (id);


--
-- Name: proc_unexpected_receipts proc_unexpected_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_unexpected_receipts
    ADD CONSTRAINT proc_unexpected_receipts_pkey PRIMARY KEY (id);


--
-- Name: proc_vendors proc_vendors_code_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_vendors
    ADD CONSTRAINT proc_vendors_code_key UNIQUE (code);


--
-- Name: proc_vendors proc_vendors_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_vendors
    ADD CONSTRAINT proc_vendors_pkey PRIMARY KEY (id);


--
-- Name: inv_current_stock uq_inv_stock_item_location; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_current_stock
    ADD CONSTRAINT uq_inv_stock_item_location UNIQUE (item_id, location_id);


--
-- Name: inv_sub_categories_2 uq_inv_sub_category_2_sub_code; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories_2
    ADD CONSTRAINT uq_inv_sub_category_2_sub_code UNIQUE (sub_category_id, short_code);


--
-- Name: inv_sub_categories_2 uq_inv_sub_category_2_sub_name; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories_2
    ADD CONSTRAINT uq_inv_sub_category_2_sub_name UNIQUE (sub_category_id, name);


--
-- Name: inv_sub_categories uq_inv_sub_category_cat_code; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories
    ADD CONSTRAINT uq_inv_sub_category_cat_code UNIQUE (category_id, short_code);


--
-- Name: inv_sub_categories uq_inv_sub_category_cat_name; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories
    ADD CONSTRAINT uq_inv_sub_category_cat_name UNIQUE (category_id, name);


--
-- Name: refresh_tokens uq_refresh_tokens_token_hash; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash);


--
-- Name: users uq_users_email; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_email UNIQUE (email);


--
-- Name: user_alert_emails user_alert_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.user_alert_emails
    ADD CONSTRAINT user_alert_emails_pkey PRIMARY KEY (user_id);


--
-- Name: checkpoint_blobs_thread_id_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs USING btree (thread_id);


--
-- Name: checkpoint_writes_thread_id_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX checkpoint_writes_thread_id_idx ON public.checkpoint_writes USING btree (thread_id);


--
-- Name: checkpoints_thread_id_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX checkpoints_thread_id_idx ON public.checkpoints USING btree (thread_id);


--
-- Name: ix_audit_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_audit_created_at ON public.agent_audit_log USING btree (created_at);


--
-- Name: ix_audit_entity; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_audit_entity ON public.agent_audit_log USING btree (entity_type, entity_id);


--
-- Name: ix_audit_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_audit_user_id ON public.agent_audit_log USING btree (user_id);


--
-- Name: ix_inv_current_stock_item_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_current_stock_item_id ON public.inv_current_stock USING btree (item_id);


--
-- Name: ix_inv_current_stock_location_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_current_stock_location_id ON public.inv_current_stock USING btree (location_id);


--
-- Name: ix_inv_items_category_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_items_category_id ON public.inv_items USING btree (category_id);


--
-- Name: ix_inv_items_is_spare; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_items_is_spare ON public.inv_items USING btree (is_spare);


--
-- Name: ix_inv_items_item_code; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_inv_items_item_code ON public.inv_items USING btree (item_code);


--
-- Name: ix_inv_items_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_inv_items_name ON public.inv_items USING btree (name);


--
-- Name: ix_inv_items_sub_category_2_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_items_sub_category_2_id ON public.inv_items USING btree (sub_category_2_id);


--
-- Name: ix_inv_items_sub_category_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_items_sub_category_id ON public.inv_items USING btree (sub_category_id);


--
-- Name: ix_inv_sub_categories_2_sub_category_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_sub_categories_2_sub_category_id ON public.inv_sub_categories_2 USING btree (sub_category_id);


--
-- Name: ix_inv_sub_categories_category_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_sub_categories_category_id ON public.inv_sub_categories USING btree (category_id);


--
-- Name: ix_inv_transactions_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_transactions_created_at ON public.inv_transactions USING btree (created_at);


--
-- Name: ix_inv_transactions_issued_to_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_transactions_issued_to_id ON public.inv_transactions USING btree (issued_to_id);


--
-- Name: ix_inv_transactions_item_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_transactions_item_id ON public.inv_transactions USING btree (item_id);


--
-- Name: ix_inv_transactions_location_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_transactions_location_id ON public.inv_transactions USING btree (location_id);


--
-- Name: ix_inv_transactions_reference_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_inv_transactions_reference_id ON public.inv_transactions USING btree (reference_id);


--
-- Name: ix_proc_po_line_regularisations_inv_transaction_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_line_regularisations_inv_transaction_id ON public.proc_po_line_regularisations USING btree (inv_transaction_id);


--
-- Name: ix_proc_po_line_regularisations_po_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_line_regularisations_po_id ON public.proc_po_line_regularisations USING btree (po_id);


--
-- Name: ix_proc_po_line_regularisations_po_line_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_line_regularisations_po_line_id ON public.proc_po_line_regularisations USING btree (po_line_id);


--
-- Name: ix_proc_po_line_regularisations_regularised_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_line_regularisations_regularised_at ON public.proc_po_line_regularisations USING btree (regularised_at);


--
-- Name: ix_proc_po_lines_inv_item_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_lines_inv_item_id ON public.proc_po_lines USING btree (inv_item_id);


--
-- Name: ix_proc_po_lines_is_regularised; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_lines_is_regularised ON public.proc_po_lines USING btree (is_regularised);


--
-- Name: ix_proc_po_lines_po_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_lines_po_id ON public.proc_po_lines USING btree (po_id);


--
-- Name: ix_proc_po_payment_tranches_blocks_dispatch; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_payment_tranches_blocks_dispatch ON public.proc_po_payment_tranches USING btree (blocks_dispatch);


--
-- Name: ix_proc_po_payment_tranches_cleared_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_payment_tranches_cleared_at ON public.proc_po_payment_tranches USING btree (cleared_at);


--
-- Name: ix_proc_po_payment_tranches_is_advance; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_payment_tranches_is_advance ON public.proc_po_payment_tranches USING btree (is_advance);


--
-- Name: ix_proc_po_payment_tranches_po_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_payment_tranches_po_id ON public.proc_po_payment_tranches USING btree (po_id);


--
-- Name: ix_proc_po_payment_tranches_po_id_sequence; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_payment_tranches_po_id_sequence ON public.proc_po_payment_tranches USING btree (po_id, sequence);


--
-- Name: ix_proc_po_receipts_inv_transaction_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_receipts_inv_transaction_id ON public.proc_po_receipts USING btree (inv_transaction_id);


--
-- Name: ix_proc_po_receipts_po_line_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_receipts_po_line_id ON public.proc_po_receipts USING btree (po_line_id);


--
-- Name: ix_proc_po_receipts_received_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_po_receipts_received_at ON public.proc_po_receipts USING btree (received_at);


--
-- Name: ix_proc_purchase_orders_placed_on; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_purchase_orders_placed_on ON public.proc_purchase_orders USING btree (placed_on);


--
-- Name: ix_proc_purchase_orders_po_number; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_proc_purchase_orders_po_number ON public.proc_purchase_orders USING btree (po_number);


--
-- Name: ix_proc_purchase_orders_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_purchase_orders_status ON public.proc_purchase_orders USING btree (status);


--
-- Name: ix_proc_purchase_orders_vendor_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_purchase_orders_vendor_id ON public.proc_purchase_orders USING btree (vendor_id);


--
-- Name: ix_proc_unexpected_receipt_headers_received_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_unexpected_receipt_headers_received_at ON public.proc_unexpected_receipt_headers USING btree (received_at);


--
-- Name: ix_proc_unexpected_receipts_inv_item_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_unexpected_receipts_inv_item_id ON public.proc_unexpected_receipts USING btree (inv_item_id);


--
-- Name: ix_proc_unexpected_receipts_inv_transaction_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_unexpected_receipts_inv_transaction_id ON public.proc_unexpected_receipts USING btree (inv_transaction_id);


--
-- Name: ix_proc_unexpected_receipts_received_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_unexpected_receipts_received_at ON public.proc_unexpected_receipts USING btree (received_at);


--
-- Name: ix_proc_unexpected_receipts_resolved_po_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_proc_unexpected_receipts_resolved_po_id ON public.proc_unexpected_receipts USING btree (resolved_po_id);


--
-- Name: ix_proc_vendors_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX ix_proc_vendors_name ON public.proc_vendors USING btree (name);


--
-- Name: ix_refresh_tokens_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_refresh_tokens_user_id ON public.refresh_tokens USING btree (user_id);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: agent_audit_log fk_audit_user; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.agent_audit_log
    ADD CONSTRAINT fk_audit_user FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: refresh_tokens fk_refresh_tokens_user_id; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT fk_refresh_tokens_user_id FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: users fk_users_created_by; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT fk_users_created_by FOREIGN KEY (created_by) REFERENCES public.users(id);


--
-- Name: inv_current_stock inv_current_stock_location_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_current_stock
    ADD CONSTRAINT inv_current_stock_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.inv_locations(id) ON DELETE CASCADE;


--
-- Name: inv_items inv_items_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_items
    ADD CONSTRAINT inv_items_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.inv_categories(id) ON DELETE SET NULL;


--
-- Name: inv_items inv_items_sub_category_2_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_items
    ADD CONSTRAINT inv_items_sub_category_2_id_fkey FOREIGN KEY (sub_category_2_id) REFERENCES public.inv_sub_categories_2(id) ON DELETE SET NULL;


--
-- Name: inv_items inv_items_sub_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_items
    ADD CONSTRAINT inv_items_sub_category_id_fkey FOREIGN KEY (sub_category_id) REFERENCES public.inv_sub_categories(id) ON DELETE SET NULL;


--
-- Name: inv_sub_categories_2 inv_sub_categories_2_sub_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories_2
    ADD CONSTRAINT inv_sub_categories_2_sub_category_id_fkey FOREIGN KEY (sub_category_id) REFERENCES public.inv_sub_categories(id) ON DELETE RESTRICT;


--
-- Name: inv_sub_categories inv_sub_categories_category_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_sub_categories
    ADD CONSTRAINT inv_sub_categories_category_id_fkey FOREIGN KEY (category_id) REFERENCES public.inv_categories(id) ON DELETE RESTRICT;


--
-- Name: inv_transactions inv_transactions_location_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.inv_transactions
    ADD CONSTRAINT inv_transactions_location_id_fkey FOREIGN KEY (location_id) REFERENCES public.inv_locations(id) ON DELETE RESTRICT;


--
-- Name: po_template_lines po_template_lines_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.po_template_lines
    ADD CONSTRAINT po_template_lines_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.po_templates(id) ON DELETE CASCADE;


--
-- Name: proc_po_line_regularisations proc_po_line_regularisations_po_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_line_regularisations
    ADD CONSTRAINT proc_po_line_regularisations_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.proc_purchase_orders(id) ON DELETE CASCADE;


--
-- Name: proc_po_line_regularisations proc_po_line_regularisations_po_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_line_regularisations
    ADD CONSTRAINT proc_po_line_regularisations_po_line_id_fkey FOREIGN KEY (po_line_id) REFERENCES public.proc_po_lines(id) ON DELETE CASCADE;


--
-- Name: proc_po_lines proc_po_lines_po_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_lines
    ADD CONSTRAINT proc_po_lines_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.proc_purchase_orders(id) ON DELETE CASCADE;


--
-- Name: proc_po_receipts proc_po_receipts_po_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_po_receipts
    ADD CONSTRAINT proc_po_receipts_po_line_id_fkey FOREIGN KEY (po_line_id) REFERENCES public.proc_po_lines(id) ON DELETE CASCADE;


--
-- Name: proc_purchase_orders proc_purchase_orders_vendor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_purchase_orders
    ADD CONSTRAINT proc_purchase_orders_vendor_id_fkey FOREIGN KEY (vendor_id) REFERENCES public.proc_vendors(id) ON DELETE RESTRICT;


--
-- Name: proc_unexpected_receipts proc_unexpected_receipts_header_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_unexpected_receipts
    ADD CONSTRAINT proc_unexpected_receipts_header_id_fkey FOREIGN KEY (header_id) REFERENCES public.proc_unexpected_receipt_headers(id) ON DELETE CASCADE;


--
-- Name: proc_unexpected_receipts proc_unexpected_receipts_resolved_po_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.proc_unexpected_receipts
    ADD CONSTRAINT proc_unexpected_receipts_resolved_po_id_fkey FOREIGN KEY (resolved_po_id) REFERENCES public.proc_purchase_orders(id) ON DELETE SET NULL;


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: postgres
--

REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO bi_agent_ro;
GRANT USAGE ON SCHEMA public TO rag_owner;
GRANT USAGE ON SCHEMA public TO rag_procurement;
GRANT USAGE ON SCHEMA public TO rag_inventory;
GRANT USAGE ON SCHEMA public TO readonly_agent;


--
-- Name: TABLE agent_audit_log; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.agent_audit_log TO bi_agent_ro;
GRANT SELECT ON TABLE public.agent_audit_log TO readonly_agent;


--
-- Name: TABLE alembic_version; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.alembic_version TO bi_agent_ro;
GRANT SELECT ON TABLE public.alembic_version TO rag_owner;
GRANT SELECT ON TABLE public.alembic_version TO readonly_agent;


--
-- Name: TABLE checkpoint_blobs; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.checkpoint_blobs TO bi_agent_ro;
GRANT SELECT ON TABLE public.checkpoint_blobs TO rag_owner;
GRANT SELECT ON TABLE public.checkpoint_blobs TO readonly_agent;


--
-- Name: TABLE checkpoint_migrations; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.checkpoint_migrations TO bi_agent_ro;
GRANT SELECT ON TABLE public.checkpoint_migrations TO rag_owner;
GRANT SELECT ON TABLE public.checkpoint_migrations TO readonly_agent;


--
-- Name: TABLE checkpoint_writes; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.checkpoint_writes TO bi_agent_ro;
GRANT SELECT ON TABLE public.checkpoint_writes TO rag_owner;
GRANT SELECT ON TABLE public.checkpoint_writes TO readonly_agent;


--
-- Name: TABLE checkpoints; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.checkpoints TO bi_agent_ro;
GRANT SELECT ON TABLE public.checkpoints TO rag_owner;
GRANT SELECT ON TABLE public.checkpoints TO readonly_agent;


--
-- Name: TABLE inv_categories; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_categories TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_categories TO rag_owner;
GRANT SELECT ON TABLE public.inv_categories TO rag_inventory;
GRANT SELECT ON TABLE public.inv_categories TO readonly_agent;


--
-- Name: TABLE inv_current_stock; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_current_stock TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_current_stock TO rag_owner;
GRANT SELECT ON TABLE public.inv_current_stock TO rag_inventory;
GRANT SELECT ON TABLE public.inv_current_stock TO readonly_agent;


--
-- Name: TABLE inv_field_definitions; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_field_definitions TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_field_definitions TO rag_owner;
GRANT SELECT ON TABLE public.inv_field_definitions TO rag_inventory;
GRANT SELECT ON TABLE public.inv_field_definitions TO readonly_agent;


--
-- Name: TABLE inv_issued_to_targets; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_issued_to_targets TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_issued_to_targets TO rag_owner;
GRANT SELECT ON TABLE public.inv_issued_to_targets TO rag_inventory;
GRANT SELECT ON TABLE public.inv_issued_to_targets TO readonly_agent;


--
-- Name: TABLE inv_items; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_items TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_items TO rag_owner;
GRANT SELECT ON TABLE public.inv_items TO rag_inventory;
GRANT SELECT ON TABLE public.inv_items TO readonly_agent;


--
-- Name: TABLE inv_locations; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_locations TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_locations TO rag_owner;
GRANT SELECT ON TABLE public.inv_locations TO rag_inventory;
GRANT SELECT ON TABLE public.inv_locations TO readonly_agent;


--
-- Name: TABLE inv_sub_categories; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_sub_categories TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_sub_categories TO rag_owner;
GRANT SELECT ON TABLE public.inv_sub_categories TO rag_inventory;
GRANT SELECT ON TABLE public.inv_sub_categories TO readonly_agent;


--
-- Name: TABLE inv_sub_categories_2; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_sub_categories_2 TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_sub_categories_2 TO rag_owner;
GRANT SELECT ON TABLE public.inv_sub_categories_2 TO rag_inventory;
GRANT SELECT ON TABLE public.inv_sub_categories_2 TO readonly_agent;


--
-- Name: TABLE inv_transactions; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.inv_transactions TO bi_agent_ro;
GRANT SELECT ON TABLE public.inv_transactions TO rag_owner;
GRANT SELECT ON TABLE public.inv_transactions TO rag_inventory;
GRANT SELECT ON TABLE public.inv_transactions TO readonly_agent;


--
-- Name: TABLE po_template_lines; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.po_template_lines TO bi_agent_ro;
GRANT SELECT ON TABLE public.po_template_lines TO rag_owner;
GRANT SELECT ON TABLE public.po_template_lines TO rag_procurement;
GRANT SELECT ON TABLE public.po_template_lines TO readonly_agent;


--
-- Name: TABLE po_templates; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.po_templates TO bi_agent_ro;
GRANT SELECT ON TABLE public.po_templates TO rag_owner;
GRANT SELECT ON TABLE public.po_templates TO rag_procurement;
GRANT SELECT ON TABLE public.po_templates TO readonly_agent;


--
-- Name: TABLE proc_po_line_regularisations; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_po_line_regularisations TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_po_line_regularisations TO rag_owner;
GRANT SELECT ON TABLE public.proc_po_line_regularisations TO rag_procurement;
GRANT SELECT ON TABLE public.proc_po_line_regularisations TO readonly_agent;


--
-- Name: TABLE proc_po_lines; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_po_lines TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_po_lines TO rag_owner;
GRANT SELECT ON TABLE public.proc_po_lines TO rag_procurement;
GRANT SELECT ON TABLE public.proc_po_lines TO readonly_agent;


--
-- Name: TABLE proc_po_payment_tranches; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_po_payment_tranches TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_po_payment_tranches TO rag_owner;
GRANT SELECT ON TABLE public.proc_po_payment_tranches TO rag_procurement;
GRANT SELECT ON TABLE public.proc_po_payment_tranches TO readonly_agent;


--
-- Name: TABLE proc_po_receipts; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_po_receipts TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_po_receipts TO rag_owner;
GRANT SELECT ON TABLE public.proc_po_receipts TO rag_procurement;
GRANT SELECT ON TABLE public.proc_po_receipts TO readonly_agent;


--
-- Name: TABLE proc_purchase_orders; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_purchase_orders TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_purchase_orders TO rag_owner;
GRANT SELECT ON TABLE public.proc_purchase_orders TO rag_procurement;
GRANT SELECT ON TABLE public.proc_purchase_orders TO readonly_agent;


--
-- Name: TABLE proc_unexpected_receipt_headers; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_unexpected_receipt_headers TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_unexpected_receipt_headers TO rag_owner;
GRANT SELECT ON TABLE public.proc_unexpected_receipt_headers TO rag_procurement;
GRANT SELECT ON TABLE public.proc_unexpected_receipt_headers TO readonly_agent;


--
-- Name: TABLE proc_unexpected_receipts; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_unexpected_receipts TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_unexpected_receipts TO rag_owner;
GRANT SELECT ON TABLE public.proc_unexpected_receipts TO rag_procurement;
GRANT SELECT ON TABLE public.proc_unexpected_receipts TO readonly_agent;


--
-- Name: TABLE proc_vendors; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.proc_vendors TO bi_agent_ro;
GRANT SELECT ON TABLE public.proc_vendors TO rag_owner;
GRANT SELECT ON TABLE public.proc_vendors TO rag_procurement;
GRANT SELECT ON TABLE public.proc_vendors TO readonly_agent;


--
-- Name: TABLE refresh_tokens; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.refresh_tokens TO bi_agent_ro;
GRANT SELECT ON TABLE public.refresh_tokens TO rag_owner;
GRANT SELECT ON TABLE public.refresh_tokens TO readonly_agent;


--
-- Name: TABLE user_alert_emails; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.user_alert_emails TO bi_agent_ro;
GRANT SELECT ON TABLE public.user_alert_emails TO rag_owner;
GRANT SELECT ON TABLE public.user_alert_emails TO readonly_agent;


--
-- Name: TABLE users; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE public.users TO bi_agent_ro;
GRANT SELECT ON TABLE public.users TO rag_owner;
GRANT SELECT ON TABLE public.users TO readonly_agent;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON SEQUENCES TO readonly_agent;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO bi_agent_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO readonly_agent;


--
-- PostgreSQL database dump complete
--

\unrestrict BpwNxURIBgchPf5VATqrwyQSbcLL1Rxt14sTj8g4V044Necl7efdMO6C72oIQUf

