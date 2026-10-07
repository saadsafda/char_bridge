### Chat Bridge

A dedicated API bridge connecting external chatbots to ERPNext securely.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app chat_bridge
```

Chat Bridge reads products, policies and customer tokens from the **Cotton Valley** app, so install it on a site that has `cotton_valley`.

### E-commerce Chatbot

An AI support assistant for the Cotton Valley / UDC storefronts. Customers can ask about products, stock and prices, their orders, shipping and returns, the privacy policy and terms, and store contact details. The bot looks these up live from ERPNext. It does not answer from memory.

#### Setup (Desk)

1. Open **Chatbot Settings**.
2. Choose a **Provider** (OpenAI, Anthropic, Google Gemini, or any OpenAI-compatible API such as Groq, DeepSeek, OpenRouter or Ollama) and paste the **API Key**. The **Model** field then loads every chat model that key can use, newest first. Pick one, or type a model ID. Use **Refresh Model List** to reload it (the list is cached for an hour).
3. Click **Test Connection**, then tick **Enable Chatbot** and save.
4. Optional: set **Product URL Template** (e.g. `https://cottonvalley.com/product/{slug}`) so the bot can link to products, and add **Chatbot FAQ** entries for answers the bot should always know (payment methods, account opening, etc.).

Conversations are saved in **Chatbot Conversation**. Turn this off with *Save Conversations*.

#### Semantic search (RAG)

Tick **Enable Semantic Search** in Chatbot Settings so the bot finds products and answers by meaning, not just exact words: typos ("bath towle"), synonyms ("something to dry my hands") and other languages work.

- **Embedding provider:** OpenAI (`text-embedding-3-small`), Google Gemini (`gemini-embedding-001`), Voyage AI (`voyage-3.5`) or any OpenAI-compatible server such as Ollama (`nomic-embed-text`). Anthropic has no embeddings API, so Claude users need one of these as well. Leave the embedding key empty to reuse the chat key when both use the same provider.
- **What gets indexed:** every visible product, every enabled Chatbot FAQ, the Privacy / Shipping & Returns / Terms pages split into passages, and the About and Contact pages. This happens per company, with UDC pages for UDC. Vectors are stored in **Chatbot Embedding**.
- **Keeping it fresh:** saving the settings starts indexing in the background (on the `long` queue, so a worker must be running). After that, changed products, policies and pages are re-embedded every 30 minutes, and FAQ changes right away. Only changed text is sent to the embedding API. **Rebuild Index** forces an update now, and *Semantic Search → Full Re-index* rebuilds from scratch (needed only if something looks wrong).
- **How answers use it:**
  - Product search is *hybrid*: keyword and meaning matches are merged, so exact names still rank first.
  - For each question, the most relevant FAQ and policy passages (*Knowledge Snippets per Question*) are attached automatically.
  - The bot can also call `search_store_knowledge` for follow-ups.
- **If the embedding API is down:** the bot falls back to keyword search and the FAQ-in-prompt behaviour, so it keeps working.

Vectors are searched in memory with numpy (about 16 MB per worker for 8,000 products at 512 dimensions), so no vector database or special MariaDB version is needed.

#### What the bot can look up

| Tool | Data |
| --- | --- |
| `search_products`, `get_product_details`, `list_categories` | Items (only visible items for the company), categories, stock status, prices |
| `search_store_knowledge` | Semantic search over FAQ, policies and store pages (when RAG is enabled) |
| `get_store_policy` | Privacy Policy / Shipping and Return / Terms and Condition pages (UDC versions for UDC) |
| `get_store_info` | Contact Page + Website Theme Settings (phone, email, hours, minimum order, free-shipping threshold) |
| `get_my_orders`, `get_order_status` | Only for logged-in customers, and only their own Sales Orders |

Pricing follows the website's rules. Logged-in customers see their own price list (falling back to Retail). Guests are told to log in, unless *Show Retail Prices to Guests* is ticked. The bot never reveals exact stock quantities.

#### API

Base: `https://<erp-site>/api/method/chat_bridge.api.chat.<method>`

Send the website's customer token as `Customer-Authorization: Bearer <token>` (the same header the other Cotton Valley APIs use) so the bot knows who the customer is. Without it the customer is a guest.

| Method | HTTP | Params | Returns |
| --- | --- | --- | --- |
| `get_config` | GET | `company` | `{enabled, bot_name, welcome_message, suggested_questions[], max_message_length}` |
| `send_message` | POST | `message`, `session_id` (omit on first message), `company` | `{session_id, reply, products[], error}` |
| `get_history` | GET | `session_id`, `company` | `{session_id, messages[{role, content, timestamp}]}` |

- `company` is `Cotton Valley` or `UDC` (defaults to *Default Company* in settings, then Cotton Valley).
- Store the returned `session_id` (e.g. in `localStorage`) and send it with every message to keep the conversation going. A session is tied to one company and, once a customer is logged in, to that customer.
- `reply` is Markdown. `products` has cards for the products mentioned in the reply: `{id, name, slug, thumbnail, price, currency, stock_status, url}`.
- `send_message` is rate-limited per IP (*Messages per Hour* in settings) and returns HTTP 429 when the limit is exceeded.

Example (Next.js):

```ts
const res = await fetch(`${ERP_URL}/api/method/chat_bridge.api.chat.send_message`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    ...(token ? { "Customer-Authorization": `Bearer ${token}` } : {}),
  },
  body: JSON.stringify({ message, session_id: sessionId, company: "Cotton Valley" }),
});
const { message: data } = await res.json(); // { session_id, reply, products, error }
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/chat_bridge
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
