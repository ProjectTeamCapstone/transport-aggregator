# NaijaFares analytics dashboard.
#
# Reads PostgreSQL DIRECTLY, not through the API. That is deliberate: the API
# serves current prices for travellers, while this answers historical questions
# for us. Routing analytics through the search API would mean building endpoints
# nobody's product needs, and would put heavy aggregate queries on the same
# service that has to answer a search in under 100ms.
#
#   Rscript -e "shiny::runApp('dashboard', port = 3838, host = '0.0.0.0')"

library(shiny)
library(DBI)
library(RPostgres)
library(ggplot2)
library(dplyr)

# ------------------------------------------------------------- connection ---

db_config <- list(
  host     = Sys.getenv("POSTGRES_HOST", "localhost"),
  port     = as.integer(Sys.getenv("POSTGRES_PORT", "5432")),
  dbname   = Sys.getenv("POSTGRES_DB", "naijafares"),
  user     = Sys.getenv("POSTGRES_USER", "naijafares"),
  password = Sys.getenv("POSTGRES_PASSWORD", "naijafares_dev")
)

# A fresh connection per query, closed on exit. Shiny sessions can outlive a
# pooled handle, and a silently dead connection is a confusing failure.
query <- function(sql, params = NULL) {
  conn <- tryCatch(
    do.call(dbConnect, c(list(drv = Postgres()), db_config)),
    error = function(e) NULL
  )
  if (is.null(conn)) return(NULL)
  on.exit(dbDisconnect(conn), add = TRUE)
  tryCatch(dbGetQuery(conn, sql, params = params), error = function(e) NULL)
}

theme_naijafares <- theme_minimal(base_size = 13) +
  theme(panel.grid.minor = element_blank(),
        plot.title = element_text(face = "bold", size = 15),
        plot.subtitle = element_text(colour = "grey35"))

empty_plot <- function(message) {
  ggplot() + annotate("text", x = 0, y = 0, label = message,
                      size = 5, colour = "grey40") +
    theme_void()
}

# ---------------------------------------------------------------------- UI ---

ui <- fluidPage(
  titlePanel("NaijaFares — price history and model performance"),
  tags$p(style = "color:#5b6472;margin-top:-10px",
         "Historical prices for the four routes, read directly from the ",
         "append-only price history."),

  sidebarLayout(
    sidebarPanel(
      width = 3,
      selectInput("route", "Route",
                  choices = c("Lagos to Abuja" = "LOS-ABV",
                              "Abuja to Lagos" = "ABV-LOS",
                              "Lagos to Onitsha" = "LOS-ONI",
                              "Lagos to Port Harcourt" = "LOS-PHC",
                              "Lagos to London" = "LOS-LHR")),
      sliderInput("days", "Days of history", min = 1, max = 45, value = 14),
      checkboxInput("split_mode", "Separate coach and air", TRUE),
      tags$hr(),
      tags$small(style = "color:#5b6472",
                 "Prices are simulated. The model was trained on that ",
                 "simulated history, so accuracy here measures whether it ",
                 "recovers the simulator's rules — not whether it predicts ",
                 "real Nigerian fares.")
    ),

    mainPanel(
      width = 9,
      tabsetPanel(
        tabPanel("Price history",
                 plotOutput("price_history", height = "380px"),
                 tags$p(style = "color:#5b6472;font-size:14px",
                        "Each line is one carrier's average price on the ",
                        "chosen route, by day.")),
        tabPanel("Carrier comparison",
                 plotOutput("carrier_comparison", height = "380px"),
                 tableOutput("carrier_table")),
        tabPanel("Fares as departure nears",
                 plotOutput("urgency_curve", height = "380px"),
                 tags$p(style = "color:#5b6472;font-size:14px",
                        "The effect the prediction model is learning: fares ",
                        "climb as the departure date approaches.")),
        tabPanel("Model performance",
                 uiOutput("model_summary"),
                 plotOutput("model_accuracy", height = "320px"))
      )
    )
  )
)

# ------------------------------------------------------------------ server ---

server <- function(input, output, session) {

  route_parts <- reactive(strsplit(input$route, "-")[[1]])

  history <- reactive({
    parts <- route_parts()
    query("
      SELECT carrier, mode, depart_time, price_ngn, seats_left, fetched_at
      FROM offers_history
      WHERE origin = $1 AND destination = $2
        AND fetched_at >= now() - make_interval(days => $3)
    ", params = list(parts[1], parts[2], input$days))
  })

  output$price_history <- renderPlot({
    data <- history()
    if (is.null(data)) return(empty_plot("Cannot reach the database."))
    if (nrow(data) == 0) return(empty_plot("No price history for this route yet."))

    daily <- data %>%
      mutate(day = as.Date(fetched_at)) %>%
      group_by(day, carrier) %>%
      summarise(price = mean(price_ngn), .groups = "drop")

    ggplot(daily, aes(day, price, colour = carrier)) +
      geom_line(linewidth = 0.9) + geom_point(size = 1.5) +
      scale_y_continuous(labels = function(x) paste0("N", format(x, big.mark = ",", scientific = FALSE))) +
      labs(title = paste("Average price —", input$route),
           subtitle = "One line per carrier", x = NULL, y = NULL, colour = NULL) +
      theme_naijafares
  })

  output$carrier_comparison <- renderPlot({
    data <- history()
    if (is.null(data) || nrow(data) == 0)
      return(empty_plot("No price history for this route yet."))

    plot <- ggplot(data, aes(reorder(carrier, price_ngn, FUN = median), price_ngn,
                             fill = if (input$split_mode) mode else carrier)) +
      geom_boxplot(alpha = 0.85, outlier.size = 0.6) +
      coord_flip() +
      scale_y_continuous(labels = function(x) paste0("N", format(x, big.mark = ",", scientific = FALSE))) +
      labs(title = "Price spread by carrier",
           subtitle = "The box covers the middle half of observed prices",
           x = NULL, y = NULL, fill = NULL) +
      theme_naijafares
    plot
  })

  output$carrier_table <- renderTable({
    data <- history()
    if (is.null(data) || nrow(data) == 0) return(NULL)
    data %>%
      group_by(Carrier = carrier, Mode = mode) %>%
      summarise(Observations = n(),
                `Cheapest seen` = min(price_ngn),
                `Typical` = median(price_ngn),
                `Dearest seen` = max(price_ngn), .groups = "drop") %>%
      arrange(Typical)
  }, digits = 0)

  output$urgency_curve <- renderPlot({
    data <- history()
    if (is.null(data) || nrow(data) == 0)
      return(empty_plot("No price history for this route yet."))

    curve <- data %>%
      mutate(days_ahead = round(as.numeric(difftime(depart_time, fetched_at, units = "days")))) %>%
      filter(days_ahead >= 0, days_ahead <= 30) %>%
      group_by(days_ahead, mode) %>%
      summarise(price = mean(price_ngn), .groups = "drop")

    ggplot(curve, aes(days_ahead, price, colour = mode)) +
      geom_line(linewidth = 1) +
      scale_x_reverse() +
      scale_y_continuous(labels = function(x) paste0("N", format(x, big.mark = ",", scientific = FALSE))) +
      labs(title = "Average fare by days to departure",
           subtitle = "Read right to left: the departure date approaches",
           x = "Days before departure", y = NULL, colour = NULL) +
      theme_naijafares
  })

  predictions <- reactive({
    query("SELECT predicted_at, will_rise, probability, actual_rose, model_version
           FROM predictions ORDER BY predicted_at")
  })

  output$model_summary <- renderUI({
    data <- predictions()
    if (is.null(data)) return(tags$p("Cannot reach the database."))

    if (nrow(data) == 0) {
      return(tagList(
        tags$h4("No predictions recorded yet"),
        tags$p(style = "color:#5b6472",
               "Accuracy over time can only be shown once predictions have ",
               "been made AND their 24-hour outcome is known. The held-out ",
               "score from training is in the session 4 report; this panel ",
               "fills in as the live endpoint is used.")
      ))
    }

    scored <- data %>% filter(!is.na(actual_rose))
    tagList(
      tags$h4(sprintf("%d predictions recorded, %d with a known outcome",
                      nrow(data), nrow(scored))),
      if (nrow(scored) > 0)
        tags$p(sprintf("Correct so far: %.1f%%",
                       100 * mean(scored$will_rise == scored$actual_rose)))
      else
        tags$p(style = "color:#5b6472",
               "None have reached their 24-hour horizon yet, so none can be ",
               "marked right or wrong.")
    )
  })

  output$model_accuracy <- renderPlot({
    data <- predictions()
    if (is.null(data) || nrow(data) == 0)
      return(empty_plot("Awaiting predictions with known outcomes."))

    scored <- data %>% filter(!is.na(actual_rose))
    if (nrow(scored) == 0)
      return(empty_plot("No prediction has reached its 24-hour horizon yet."))

    scored %>%
      mutate(day = as.Date(predicted_at)) %>%
      group_by(day) %>%
      summarise(accuracy = mean(will_rise == actual_rose), n = n(), .groups = "drop") %>%
      ggplot(aes(day, accuracy)) +
      geom_line(linewidth = 1, colour = "#0b5fa5") +
      geom_point(aes(size = n), colour = "#0b5fa5") +
      geom_hline(yintercept = 0.5, linetype = "dashed", colour = "grey50") +
      scale_y_continuous(limits = c(0, 1), labels = scales::percent) +
      labs(title = "Prediction accuracy over time",
           subtitle = "Dashed line is a coin toss", x = NULL, y = NULL, size = "Predictions") +
      theme_naijafares
  })
}

shinyApp(ui, server)
