--
-- PostgreSQL database dump
--


-- Dumped from database version 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)
-- Dumped by pg_dump version 18.6 (Ubuntu 18.6-0ubuntu0.26.04.1)




--
-- Name: aliases; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.aliases (
    chef_id bigint DEFAULT 0 NOT NULL,
    phrase text NOT NULL,
    catalog_id integer NOT NULL,
    hits integer DEFAULT 0 NOT NULL,
    updated_at text NOT NULL
);



--
-- Name: claims; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.claims (
    id bigint NOT NULL,
    order_id integer NOT NULL,
    user_id bigint,
    text text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at text NOT NULL
);



--
-- Name: claims_id_seq; Type: SEQUENCE; Schema: public; Owner: order_bot
--

CREATE SEQUENCE public.claims_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: claims_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: order_bot
--

ALTER SEQUENCE public.claims_id_seq OWNED BY public.claims.id;


--
-- Name: drafts; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.drafts (
    id text NOT NULL,
    chat_id bigint NOT NULL,
    user_id bigint NOT NULL,
    payload text NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    updated_at text NOT NULL
);



--
-- Name: missing_items; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.missing_items (
    phrase text NOT NULL,
    count integer DEFAULT 0 NOT NULL,
    last_seen text NOT NULL
);



--
-- Name: order_items; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.order_items (
    id integer NOT NULL,
    order_id integer NOT NULL,
    catalog_id integer,
    name text,
    qty double precision,
    unit text,
    price double precision,
    total double precision,
    raw text,
    status text,
    stock_note text
);



--
-- Name: order_items_id_seq; Type: SEQUENCE; Schema: public; Owner: order_bot
--

CREATE SEQUENCE public.order_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: order_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: order_bot
--

ALTER SEQUENCE public.order_items_id_seq OWNED BY public.order_items.id;


--
-- Name: orders; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.orders (
    id integer NOT NULL,
    created_at text NOT NULL,
    chef_id bigint,
    chef_name text,
    chef_username text,
    manager_chat_id bigint,
    delivery_date text,
    delivery_time text,
    comment text,
    total double precision DEFAULT 0,
    status text DEFAULT 'new'::text,
    source text,
    raw_text text,
    notified_at text,
    questions integer DEFAULT 0 NOT NULL,
    chef_card_message_id bigint,
    manager_card_message_id bigint,
    delivery_address text,
    delivery_phone text,
    payment_method text DEFAULT 'card_visa'::text,
    payment_status text DEFAULT 'unpaid'::text NOT NULL,
    paid_amount double precision,
    paid_at text,
    packed_at text,
    delivering_at text,
    delivered_at text,
    courier_chat_id bigint,
    courier_card_message_id bigint,
    courier_notified_at text,
    delivery_pin text,
    rating integer,
    photo_file_id text
);



--
-- Name: orders_id_seq; Type: SEQUENCE; Schema: public; Owner: order_bot
--

CREATE SEQUENCE public.orders_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: order_bot
--

ALTER SEQUENCE public.orders_id_seq OWNED BY public.orders.id;


--
-- Name: price_versions; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.price_versions (
    id integer NOT NULL,
    created_at text NOT NULL,
    path text NOT NULL,
    summary text,
    items integer DEFAULT 0 NOT NULL
);



--
-- Name: price_versions_id_seq; Type: SEQUENCE; Schema: public; Owner: order_bot
--

CREATE SEQUENCE public.price_versions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: price_versions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: order_bot
--

ALTER SEQUENCE public.price_versions_id_seq OWNED BY public.price_versions.id;


--
-- Name: question_log; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.question_log (
    id integer NOT NULL,
    phrase text NOT NULL,
    kind text NOT NULL,
    created_at text NOT NULL
);



--
-- Name: question_log_id_seq; Type: SEQUENCE; Schema: public; Owner: order_bot
--

CREATE SEQUENCE public.question_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: question_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: order_bot
--

ALTER SEQUENCE public.question_log_id_seq OWNED BY public.question_log.id;


--
-- Name: schema_version; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.schema_version (
    version integer NOT NULL,
    applied_at text NOT NULL
);



--
-- Name: settings; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.settings (
    key text NOT NULL,
    value text
);



--
-- Name: staff; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.staff (
    user_id bigint NOT NULL,
    role text NOT NULL,
    name text,
    active integer DEFAULT 1 NOT NULL,
    created_at text NOT NULL
);



--
-- Name: stock_items; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.stock_items (
    catalog_id integer NOT NULL,
    available integer DEFAULT 1 NOT NULL,
    stock_left double precision,
    updated_at text NOT NULL
);



--
-- Name: ton_payments; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.ton_payments (
    id bigint NOT NULL,
    order_id bigint NOT NULL,
    comment text NOT NULL,
    amount_usdt text NOT NULL,
    amount_nano bigint NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    tx_hash text,
    created_at text NOT NULL,
    expires_at text NOT NULL,
    paid_at text
);



--
-- Name: ton_payments_id_seq; Type: SEQUENCE; Schema: public; Owner: order_bot
--

CREATE SEQUENCE public.ton_payments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



--
-- Name: ton_payments_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: order_bot
--

ALTER SEQUENCE public.ton_payments_id_seq OWNED BY public.ton_payments.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: order_bot
--

CREATE TABLE public.users (
    user_id bigint NOT NULL,
    name text,
    username text,
    venue text,
    access text DEFAULT 'pending'::text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    phone text,
    address text
);



--
-- Name: claims id; Type: DEFAULT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.claims ALTER COLUMN id SET DEFAULT nextval('public.claims_id_seq'::regclass);


--
-- Name: order_items id; Type: DEFAULT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.order_items ALTER COLUMN id SET DEFAULT nextval('public.order_items_id_seq'::regclass);


--
-- Name: orders id; Type: DEFAULT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.orders ALTER COLUMN id SET DEFAULT nextval('public.orders_id_seq'::regclass);


--
-- Name: price_versions id; Type: DEFAULT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.price_versions ALTER COLUMN id SET DEFAULT nextval('public.price_versions_id_seq'::regclass);


--
-- Name: question_log id; Type: DEFAULT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.question_log ALTER COLUMN id SET DEFAULT nextval('public.question_log_id_seq'::regclass);


--
-- Name: ton_payments id; Type: DEFAULT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.ton_payments ALTER COLUMN id SET DEFAULT nextval('public.ton_payments_id_seq'::regclass);


--
-- Name: aliases aliases_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.aliases
    ADD CONSTRAINT aliases_pkey PRIMARY KEY (chef_id, phrase);


--
-- Name: claims claims_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.claims
    ADD CONSTRAINT claims_pkey PRIMARY KEY (id);


--
-- Name: drafts drafts_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.drafts
    ADD CONSTRAINT drafts_pkey PRIMARY KEY (id);


--
-- Name: missing_items missing_items_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.missing_items
    ADD CONSTRAINT missing_items_pkey PRIMARY KEY (phrase);


--
-- Name: order_items order_items_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: price_versions price_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.price_versions
    ADD CONSTRAINT price_versions_pkey PRIMARY KEY (id);


--
-- Name: question_log question_log_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.question_log
    ADD CONSTRAINT question_log_pkey PRIMARY KEY (id);


--
-- Name: schema_version schema_version_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.schema_version
    ADD CONSTRAINT schema_version_pkey PRIMARY KEY (version);


--
-- Name: settings settings_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.settings
    ADD CONSTRAINT settings_pkey PRIMARY KEY (key);


--
-- Name: staff staff_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.staff
    ADD CONSTRAINT staff_pkey PRIMARY KEY (user_id);


--
-- Name: stock_items stock_items_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.stock_items
    ADD CONSTRAINT stock_items_pkey PRIMARY KEY (catalog_id);


--
-- Name: ton_payments ton_payments_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.ton_payments
    ADD CONSTRAINT ton_payments_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);


--
-- Name: idx_drafts_chat_status; Type: INDEX; Schema: public; Owner: order_bot
--

CREATE INDEX idx_drafts_chat_status ON public.drafts USING btree (chat_id, status);


--
-- Name: idx_missing_last_seen; Type: INDEX; Schema: public; Owner: order_bot
--

CREATE INDEX idx_missing_last_seen ON public.missing_items USING btree (last_seen);


--
-- Name: idx_order_items_order; Type: INDEX; Schema: public; Owner: order_bot
--

CREATE INDEX idx_order_items_order ON public.order_items USING btree (order_id);


--
-- Name: idx_question_log_created; Type: INDEX; Schema: public; Owner: order_bot
--

CREATE INDEX idx_question_log_created ON public.question_log USING btree (created_at);


--
-- Name: idx_ton_pending; Type: INDEX; Schema: public; Owner: order_bot
--

CREATE INDEX idx_ton_pending ON public.ton_payments USING btree (status, order_id);


--
-- Name: idx_ton_tx; Type: INDEX; Schema: public; Owner: order_bot
--

CREATE UNIQUE INDEX idx_ton_tx ON public.ton_payments USING btree (tx_hash);


--
-- Name: order_items order_items_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: order_bot
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


