"""Ventana principal de MouserEngine."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QModelIndex, QSettings, QSize, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_TITLE, __version__
from ..bom import BomError, BomTable, build_items, keyword_for, load_table
from ..cart import CartLine, CartResult, cart_lines
from ..config import Settings
from ..export import export_cart_csv, export_excel
from ..formatting import fmt_int, fmt_money, fmt_num
from ..fx import fetch_clp_rate
from ..history import ComparisonRow, HistoryEntry, HistoryError, HistoryStore, PriceRecord, Snapshot, compare_quotes
from ..models import LEVEL_EXCLUDED, BomItem, ItemQuote, Part, QuoteParams, QuoteSummary
from ..mouser_api import (
    DAILY_LIMIT,
    MAX_CART_ITEMS,
    MouserAuthError,
    MouserCancelled,
    MouserClient,
    MouserConnectionError,
    MouserRateLimitError,
    RateLimiter,
)
from ..lookup import lookup_all
from ..passives import Defaults, parse_spec
from ..quote import apply_lookup, mark_specs_disabled, queries_for, quote_all, specs_for, summarize
from .cart_dialogs import CartConfirmDialog, CartResultDialog
from .detail_panel import DetailPanel
from .dialogs import ImportDialog, SearchDialog, SettingsDialog
from .history_dialogs import ComparisonDialog, HistoryDialog, fmt_when
from .icons import cart_icon
from .items_model import COL, COLUMNS, STATUS_FILTERS, ItemsFilterModel, ItemsModel
from .scenarios_panel import ScenariosPanel
from .workers import Worker, WorkerPool

BOM_FILTER = "BOM (*.xlsx *.xlsm *.xls *.csv *.txt *.tsv);;Todos los archivos (*)"
BOM_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".tsv"}


class SummaryCard(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        self.value = QLabel("—")
        self.value.setObjectName("CardValue")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sub = QLabel("")
        self.sub.setObjectName("CardSub")
        self.sub.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)
        layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(150)

    def set(self, value: str, sub: str = "") -> None:
        self.value.setText(value)
        self.sub.setText(sub)


class DropZone(QFrame):
    def __init__(self, on_open: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setObjectName("DropZone")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        icon = QLabel()
        icon.setPixmap(self.style().standardIcon(QStyle.SP_FileDialogContentsView).pixmap(56, 56))
        icon.setAlignment(Qt.AlignCenter)
        title = QLabel("Arrastre aquí su BOM")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 16pt; font-weight: 600; color: #1F2328;")
        hint = QLabel("Excel (.xlsx) o CSV con número de parte (MPN o código Mouser) y cantidad por placa.\n"
                      "Las columnas se detectan automáticamente.")
        hint.setAlignment(Qt.AlignCenter)
        hint.setObjectName("Hint")
        button = QPushButton("Abrir BOM…")
        button.setObjectName("Primary")
        button.setMinimumWidth(160)
        button.clicked.connect(on_open)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addSpacing(10)
        layout.addWidget(button, 0, Qt.AlignCenter)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None,
                 client_factory: Callable[..., MouserClient] | None = None,
                 persist: bool = True):
        super().__init__()
        self.settings = settings or Settings.load()
        self.persist = persist
        self.interactive = True  # False en pruebas automáticas: no abre diálogos modales
        self.rate_limiter = RateLimiter()
        self._client_factory = client_factory
        self.workers = WorkerPool()
        self.table_doc: BomTable | None = None
        self.items: list[BomItem] = []
        self.quotes: list[ItemQuote] = []
        self.summary = QuoteSummary()
        self.params = QuoteParams()
        self.last_query: datetime | None = None
        self._lookup_worker: Worker | None = None
        self._lookup_targets: list[BomItem] = []
        self._lookup_started = 0.0
        self._lookup_silent = False
        self._pending_requery: list[BomItem] = []
        self._connection_state = ("idle", "")
        self._items_generation = 0  # cambia al abrir otro BOM o una cotización del historial
        self._lookup_generation = 0
        self._cart_worker: Worker | None = None
        self._cart_lines: list[CartLine] = []
        self._cart_snapshot: Snapshot | None = None
        self.last_cart: CartResult | None = None
        self.history = HistoryStore()
        self.history_entry: HistoryEntry | None = None  # cotización del historial abierta (si la hay)
        self._compare_baseline: dict[int, ItemQuote] | None = None
        self._compare_before_total = Decimal(0)
        self.last_comparison: list[ComparisonRow] | None = None

        self.setWindowTitle(APP_TITLE)
        self.resize(1440, 900)
        self.setAcceptDrops(True)

        self.model = ItemsModel(self)
        self.proxy = ItemsFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.model.item_edited.connect(self._on_item_edited)

        self._build_actions()
        self._build_menu_and_toolbar()
        self._build_central()
        self._build_status_bar()
        self._load_params_into_widgets()

        self.recalc_timer = QTimer(self)
        self.recalc_timer.setSingleShot(True)
        self.recalc_timer.setInterval(120)
        self.recalc_timer.timeout.connect(self.recalculate)

        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto_refresh)
        self._apply_auto_refresh()

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(30_000)
        self.clock_timer.timeout.connect(self._update_query_age)
        self.clock_timer.start()

        self._restore_geometry()
        self._update_actions()
        self._update_usage()
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------------ cliente API

    def client(self, api_key: str | None = None) -> MouserClient:
        key = api_key if api_key is not None else self.settings.effective_api_key
        if self._client_factory is not None:
            return self._client_factory(key)
        return MouserClient(key, timeout=self.settings.timeout, rate_limiter=self.rate_limiter,
                            on_request=self.settings.register_call)

    def cart_client(self) -> MouserClient:
        """Cliente con la clave de Cart API (no cuenta en el límite diario de la Search API)."""
        key = self.settings.effective_cart_api_key
        if self._client_factory is not None:
            return self._client_factory(key)
        return MouserClient(key, timeout=max(30, self.settings.timeout), rate_limiter=RateLimiter())

    # ------------------------------------------------------------------ construcción

    def _icon(self, standard: QStyle.StandardPixmap) -> QIcon:
        return self.style().standardIcon(standard)

    def _build_actions(self) -> None:
        self.act_open = QAction(self._icon(QStyle.SP_DialogOpenButton), "Abrir BOM…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(lambda: self.open_bom())
        self.act_refresh = QAction(self._icon(QStyle.SP_BrowserReload), "Actualizar precios", self)
        self.act_refresh.setShortcut(QKeySequence("F5"))
        self.act_refresh.setToolTip("Consulta precios y stock actuales en Mouser (F5)")
        self.act_refresh.triggered.connect(lambda: self.start_lookup())
        self.act_cancel = QAction(self._icon(QStyle.SP_BrowserStop), "Cancelar consulta", self)
        self.act_cancel.triggered.connect(self._user_cancel)
        self.act_save_history = QAction(self._icon(QStyle.SP_DialogSaveButton), "Guardar en historial", self)
        self.act_save_history.setShortcut(QKeySequence.Save)
        self.act_save_history.setToolTip("Guarda esta cotización en el historial de este computador (Ctrl+S)")
        self.act_save_history.triggered.connect(lambda: self.save_to_history())
        self.act_history = QAction(self._icon(QStyle.SP_FileDialogListView), "Historial", self)
        self.act_history.setShortcut(QKeySequence("Ctrl+H"))
        self.act_history.setToolTip("Cotizaciones guardadas: abrirlas, compararlas con los precios de hoy o "
                                    "exportarlas (Ctrl+H)")
        self.act_history.triggered.connect(self.open_history)
        self.act_export = QAction(self._icon(QStyle.SP_DialogSaveButton), "Exportar Excel…", self)
        self.act_export.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export.triggered.connect(lambda: self.export_excel())
        self.act_cart = QAction(self._icon(QStyle.SP_FileDialogDetailedView), "Exportar carro Mouser…", self)
        self.act_cart.setToolTip("CSV con código Mouser y cantidad para cargar en el carro de mouser.com")
        self.act_cart.triggered.connect(lambda: self.export_cart())
        self.act_create_cart = QAction(cart_icon(), "Crear carro en Mouser…", self)
        self.act_create_cart.setToolTip("Crea un carro nuevo en su cuenta de Mouser con las partes de esta "
                                        "cotización (Cart API). No se hace ningún pedido.")
        self.act_create_cart.triggered.connect(lambda: self.create_cart())
        self.act_settings = QAction(self._icon(QStyle.SP_FileDialogInfoView), "Configuración…", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_settings.triggered.connect(self.open_settings)
        self.act_search = QAction(self._icon(QStyle.SP_FileDialogContentsView), "Buscar en Mouser…", self)
        self.act_search.setShortcut(QKeySequence.Find)
        self.act_search.triggered.connect(self.search_selected)
        self.act_help = QAction(self._icon(QStyle.SP_DialogHelpButton), "Cómo usar", self)
        self.act_help.setShortcut(QKeySequence.HelpContents)
        self.act_help.triggered.connect(self.show_help)
        self.act_about = QAction("Acerca de MouserEngine", self)
        self.act_about.triggered.connect(self.show_about)
        self.act_shortcut = QAction("Crear acceso directo en el escritorio", self)
        self.act_shortcut.triggered.connect(self.create_desktop_shortcut)
        self.act_quit = QAction("Salir", self)
        self.act_quit.setShortcut(QKeySequence.Quit)
        self.act_quit.triggered.connect(self.close)

    def _build_menu_and_toolbar(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&Archivo")
        file_menu.addAction(self.act_open)
        file_menu.addSeparator()
        file_menu.addAction(self.act_save_history)
        file_menu.addAction(self.act_history)
        file_menu.addSeparator()
        file_menu.addAction(self.act_export)
        file_menu.addAction(self.act_cart)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        quote_menu = menu.addMenu("&Cotización")
        quote_menu.addAction(self.act_refresh)
        quote_menu.addAction(self.act_cancel)
        quote_menu.addSeparator()
        quote_menu.addAction(self.act_search)
        quote_menu.addSeparator()
        quote_menu.addAction(self.act_create_cart)
        tools = menu.addMenu("&Herramientas")
        tools.addAction(self.act_settings)
        if sys.platform.startswith("win"):
            tools.addAction(self.act_shortcut)
        help_menu = menu.addMenu("A&yuda")
        help_menu.addAction(self.act_help)
        help_menu.addAction(self.act_about)

        toolbar = QToolBar("Principal", self)
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        for action in (self.act_open, self.act_refresh):  # «Cancelar» aparece junto a la barra de progreso
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.act_history)
        toolbar.addAction(self.act_export)
        toolbar.addAction(self.act_cart)
        toolbar.addAction(self.act_create_cart)
        toolbar.addSeparator()
        toolbar.addAction(self.act_search)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addAction(self.act_settings)
        toolbar.addAction(self.act_help)
        self.addToolBar(toolbar)

    def _build_parameters(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        quote_box = QGroupBox("Cotización")
        qform = QFormLayout(quote_box)
        self.client_edit = QLineEdit(self.settings.client_name)
        self.client_edit.setPlaceholderText("Empresa para la que se diseña")
        self.client_edit.setToolTip("Nombre del cliente: aparece en el Excel, el historial y el informe para el cliente")
        self.client_edit.editingFinished.connect(self._client_changed)
        qform.addRow("Cliente:", self.client_edit)
        layout.addWidget(quote_box)

        buy_box = QGroupBox("Cantidad a fabricar")
        form = QFormLayout(buy_box)
        form.setLabelAlignment(Qt.AlignLeft)
        self.boards_spin = QSpinBox()
        self.boards_spin.setRange(0, 10_000_000)
        self.boards_spin.setGroupSeparatorShown(True)
        self.boards_spin.setToolTip("Número de placas o equipos a fabricar")
        form.addRow("Placas / equipos:", self.boards_spin)
        self.spares_spin = self._pct_spin("Unidades extra para todas las partes (merma)")
        form.addRow("Merma general:", self.spares_spin)
        self.passive_spin = self._pct_spin("Para resistencias, condensadores, inductores y ferritas "
                                           "reemplaza a la merma general (0 = usar la general)")
        form.addRow("Merma pasivos:", self.passive_spin)
        self.optimize_check = QCheckBox("Optimizar por tramos de precio")
        self.optimize_check.setToolTip("Si comprar más unidades cuesta menos (por el siguiente tramo de precio), "
                                       "compra esa cantidad")
        form.addRow(self.optimize_check)
        layout.addWidget(buy_box)

        cost_box = QGroupBox("Costos adicionales (estimación)")
        cform = QFormLayout(cost_box)
        self.freight_spin = QDoubleSpinBox()
        self.freight_spin.setRange(0, 10_000_000)
        self.freight_spin.setDecimals(2)
        self.freight_spin.setGroupSeparatorShown(True)
        self.freight_spin.setToolTip("Costo de envío en la moneda de Mouser")
        cform.addRow("Flete:", self.freight_spin)
        self.duty_spin = self._pct_spin("Arancel de importación sobre (componentes + flete)")
        cform.addRow("Arancel:", self.duty_spin)
        self.vat_spin = self._pct_spin("IVA sobre (componentes + flete + arancel). En Chile: 19 %")
        cform.addRow("IVA:", self.vat_spin)
        fx_row = QHBoxLayout()
        fx_row.setSpacing(4)
        self.fx_spin = QDoubleSpinBox()
        self.fx_spin.setRange(0, 1_000_000)
        self.fx_spin.setDecimals(2)
        self.fx_spin.setGroupSeparatorShown(True)
        self.fx_spin.setSpecialValueText("—")
        self.fx_spin.setToolTip("Pesos chilenos por unidad de la moneda de Mouser (0 = no convertir)")
        self.fx_button = QPushButton("Hoy")
        self.fx_button.setFixedWidth(52)
        self.fx_button.setToolTip("Obtener el dólar observado del día (Banco Central de Chile, vía mindicador.cl)")
        self.fx_button.clicked.connect(self.fetch_fx)
        fx_row.addWidget(self.fx_spin, 1)
        fx_row.addWidget(self.fx_button)
        cform.addRow("Cambio a CLP:", fx_row)
        layout.addWidget(cost_box)
        for spin in (self.boards_spin, self.spares_spin, self.passive_spin, self.freight_spin,
                     self.duty_spin, self.vat_spin, self.fx_spin):
            spin.setMinimumWidth(80)
            spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        api_box = QGroupBox("Conexión con Mouser")
        alayout = QVBoxLayout(api_box)
        self.refresh_button = QPushButton("Actualizar precios ahora")
        self.refresh_button.setObjectName("Primary")
        self.refresh_button.clicked.connect(lambda: self.start_lookup())
        alayout.addWidget(self.refresh_button)
        self.connection_label = QLabel("")
        self.connection_label.setWordWrap(True)
        alayout.addWidget(self.connection_label)
        self.query_age_label = QLabel("Sin consultas todavía")
        self.query_age_label.setObjectName("Hint")
        self.query_age_label.setWordWrap(True)
        alayout.addWidget(self.query_age_label)
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("Actualizar cada:"))
        self.auto_spin = QSpinBox()
        self.auto_spin.setMinimumWidth(80)
        self.auto_spin.setRange(0, 1440)
        self.auto_spin.setSuffix(" min")
        self.auto_spin.setSpecialValueText("Nunca")
        self.auto_spin.setToolTip("Vuelve a consultar precios y stock automáticamente")
        self.auto_spin.valueChanged.connect(self._auto_spin_changed)
        auto_row.addWidget(self.auto_spin, 1)
        alayout.addLayout(auto_row)
        self.usage_label = QLabel("")
        self.usage_label.setObjectName("Hint")
        alayout.addWidget(self.usage_label)
        layout.addWidget(api_box)
        layout.addStretch(1)

        for widget in (self.boards_spin, self.spares_spin, self.passive_spin, self.freight_spin,
                       self.duty_spin, self.vat_spin, self.fx_spin):
            widget.valueChanged.connect(self.schedule_recalc)
        self.optimize_check.toggled.connect(self.schedule_recalc)

        panel.setMinimumWidth(250)
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(290)
        return scroll

    @staticmethod
    def _pct_spin(tooltip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, 1000)
        spin.setDecimals(1)
        spin.setSingleStep(1)
        spin.setSuffix(" %")
        spin.setToolTip(tooltip)
        return spin

    def _build_central(self) -> None:
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 6)
        outer.setSpacing(10)
        outer.addWidget(self._build_parameters())

        right = QVBoxLayout()
        right.setSpacing(8)
        self.banner = QLabel("")
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setOpenExternalLinks(False)
        self.banner.linkActivated.connect(self._banner_link)
        self.banner.hide()
        right.addWidget(self.banner)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.card_goods = SummaryCard("Subtotal componentes")
        self.card_savings = SummaryCard("Ahorro por tramos de precio")
        self.card_total = SummaryCard("Total estimado")
        self.card_status = SummaryCard("Estado de las partes")
        for card in (self.card_goods, self.card_savings, self.card_total, self.card_status):
            cards.addWidget(card)
        right.addLayout(cards)

        filters = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filtrar por MPN, designador, descripción, fabricante…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._filter_changed)
        self.status_filter = QComboBox()
        for label, _ in STATUS_FILTERS:
            self.status_filter.addItem(label)
        self.status_filter.currentIndexChanged.connect(self._filter_changed)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")
        filters.addWidget(self.filter_edit, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.count_label)
        right.addLayout(filters)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL["rows"], Qt.AscendingOrder)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                   | QAbstractItemView.SelectedClicked)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setHighlightSections(False)
        header.setSectionsMovable(True)
        for index, (_, _, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        header.setStretchLastSection(True)
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        self.table.selectionModel().currentRowChanged.connect(self._selection_changed)

        self.detail = DetailPanel()
        self.detail.history_provider = self._part_history
        self.detail.use_option.connect(self._use_option)
        self.detail.auto_select.connect(self._auto_select)
        self.detail.search_requested.connect(self.search_selected)
        self.detail.requery_requested.connect(self._requery_selected)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 300])
        self.splitter = splitter

        self.scenarios = ScenariosPanel(self.workers, self.settings.scenario_quantities,
                                        self.settings.scenario_max, self.settings.chart_mode)
        self.scenarios.use_quantity.connect(self._use_scenario_quantity)
        self.scenarios.settings_changed.connect(self._scenario_setting)
        self._scenarios_dirty = True

        self.main_tabs = QTabWidget()
        self.main_tabs.setDocumentMode(True)
        self.main_tabs.addTab(splitter, "Partes")
        self.main_tabs.addTab(self.scenarios, "Escenarios de volumen")
        self.main_tabs.currentChanged.connect(self._main_tab_changed)

        self.stack = QStackedWidget()
        self.stack.addWidget(DropZone(lambda: self.open_bom()))
        self.stack.addWidget(self.main_tabs)
        right.addWidget(self.stack, 1)
        outer.addLayout(right, 1)
        self.setCentralWidget(central)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.clicked.connect(self._user_cancel)
        self.cancel_button.hide()
        self.status_connection = QLabel("")
        bar.addPermanentWidget(self.progress)
        bar.addPermanentWidget(self.cancel_button)
        bar.addPermanentWidget(self.status_connection)

    # ------------------------------------------------------------------ parámetros

    def _load_params_into_widgets(self) -> None:
        s = self.settings
        self.auto_spin.blockSignals(True)
        self.auto_spin.setValue(s.auto_refresh_minutes)
        self.auto_spin.blockSignals(False)
        self._apply_params(QuoteParams(max(0, s.boards), s.spares_pct, s.passive_spares_pct, s.optimize_breaks,
                                       s.freight, s.duty_pct, s.vat_pct, s.fx_rate))

    def _apply_params(self, p: QuoteParams) -> None:
        """Muestra los parámetros en el panel izquierdo (sin recalcular todavía)."""
        widgets = (self.boards_spin, self.spares_spin, self.passive_spin, self.freight_spin, self.duty_spin,
                   self.vat_spin, self.fx_spin, self.optimize_check)
        for widget in widgets:
            widget.blockSignals(True)
        self.boards_spin.setValue(max(0, int(p.boards)))
        self.spares_spin.setValue(p.spares_pct)
        self.passive_spin.setValue(p.passive_spares_pct)
        self.optimize_check.setChecked(bool(p.optimize_breaks))
        self.freight_spin.setValue(p.freight)
        self.duty_spin.setValue(p.duty_pct)
        self.vat_spin.setValue(p.vat_pct)
        self.fx_spin.setValue(p.fx_rate)
        for widget in widgets:
            widget.blockSignals(False)
        self.params = self._read_params()

    def _read_params(self) -> QuoteParams:
        return QuoteParams(
            boards=self.boards_spin.value(),
            spares_pct=self.spares_spin.value(),
            passive_spares_pct=self.passive_spin.value(),
            optimize_breaks=self.optimize_check.isChecked(),
            freight=self.freight_spin.value(),
            duty_pct=self.duty_spin.value(),
            vat_pct=self.vat_spin.value(),
            fx_rate=self.fx_spin.value(),
        )

    def _store_params(self) -> None:
        p = self.params
        s = self.settings
        s.client_name = self.client_edit.text().strip()
        s.boards, s.spares_pct, s.passive_spares_pct = p.boards, p.spares_pct, p.passive_spares_pct
        s.optimize_breaks, s.freight, s.duty_pct, s.vat_pct, s.fx_rate = (
            p.optimize_breaks, p.freight, p.duty_pct, p.vat_pct, p.fx_rate)

    def save_settings(self) -> None:
        self._store_params()
        if self.persist:
            try:
                self.settings.save()
            except OSError:
                pass

    # ------------------------------------------------------------------ cálculo

    @Slot()
    def schedule_recalc(self, *args) -> None:
        self.recalc_timer.start()

    @Slot()
    def recalculate(self) -> None:
        self.params = self._read_params()
        self.quotes = quote_all(self.items, self.params)
        self.summary = summarize(self.items, self.quotes, self.params)
        if len(self.model.items) != len(self.items) or self.model.items is not self.items:
            self.model.set_items(self.items, self.quotes)
        else:
            self.model.set_quotes(self.quotes)
        self._refresh_filter()
        self._update_cards()
        self._refresh_detail()
        self._update_actions()
        self._scenarios_dirty = True
        if self.main_tabs.currentWidget() is self.scenarios:
            self._refresh_scenarios()

    def _refresh_scenarios(self) -> None:
        if not self.items:
            return
        self._scenarios_dirty = False
        self.scenarios.refresh(self.items, self.params, self.params.boards)

    def _main_tab_changed(self, index: int) -> None:
        if self.main_tabs.widget(index) is self.scenarios and self._scenarios_dirty:
            self._refresh_scenarios()

    def _use_scenario_quantity(self, boards: int) -> None:
        self.boards_spin.setValue(boards)
        self.statusBar().showMessage(f"Cotización actualizada a {fmt_int(boards)} placas.", 6000)

    def _scenario_setting(self, name: str, value) -> None:
        setattr(self.settings, name, value)
        self.save_settings()

    def _client_changed(self) -> None:
        self.settings.client_name = self.client_edit.text().strip()
        self.save_settings()

    def _refresh_filter(self) -> None:
        self.proxy.refresh()
        total = len(self.items)
        shown = self.proxy.rowCount()
        self.count_label.setText(f"{shown} de {total} partes" if total else "")

    def _update_cards(self) -> None:
        s = self.summary
        cur = s.currency or ""
        suffix = " (varias monedas)" if s.mixed_currency else ""
        if not self.items:
            for card in (self.card_goods, self.card_savings, self.card_total, self.card_status):
                card.set("—", "")
            return
        missing = f" · <span style='color:#CF222E'>{s.unpriced} sin precio</span>" if s.unpriced else ""
        self.card_goods.set(fmt_money(s.goods, cur) + suffix,
                            f"{s.priced} de {s.included} partes con precio{missing}")
        self.card_goods.sub.setTextFormat(Qt.RichText)
        if s.savings > 0:
            sub = ("Aplicado en el subtotal." if self.params.optimize_breaks
                   else "Active «Optimizar por tramos de precio» para aplicarlo.")
            self.card_savings.set(fmt_money(s.savings, cur), sub)
        else:
            self.card_savings.set(fmt_money(0, cur), "No hay tramos que reduzcan el costo.")
        extras = []
        if s.freight:
            extras.append("flete")
        if s.duty:
            extras.append("arancel")
        if s.vat:
            extras.append("IVA")
        sub = ("Incluye " + ", ".join(extras) + "." if extras else "Sin costos adicionales ingresados.")
        if s.total_clp is not None:
            sub = f"≈ CLP {fmt_int(s.total_clp)} · " + sub
        self.card_total.set(fmt_money(s.total, cur), sub)
        pending = f" · {s.pending} pendientes" if s.pending else ""
        self.card_status.set(
            f"{s.ok} de {s.included} OK",
            f"<span style='color:#9A6700'>{s.warn} con advertencias</span> · "
            f"<span style='color:#CF222E'>{s.error} con problemas</span>{pending}"
            + (f" · {s.excluded} excluidas" if s.excluded else ""))
        self.card_status.sub.setTextFormat(Qt.RichText)

    def _update_actions(self) -> None:
        has_items = bool(self.items)
        busy = self._lookup_worker is not None
        self.act_refresh.setEnabled(has_items and not busy)
        self.refresh_button.setEnabled(has_items and not busy)
        self.act_cancel.setEnabled(busy)
        self.act_export.setEnabled(has_items)
        self.act_save_history.setEnabled(has_items)
        can_buy = has_items and any(q.part is not None and q.buy_qty for q in self.quotes)
        self.act_cart.setEnabled(can_buy)
        self.act_create_cart.setEnabled(can_buy and not busy and self._cart_worker is None)
        self.act_search.setEnabled(has_items and self._current_item() is not None)
        self.stack.setCurrentIndex(1 if has_items else 0)

    # ------------------------------------------------------------------ selección

    def _current_source_row(self) -> int:
        index = self.table.currentIndex()
        if not index.isValid():
            return -1
        return self.proxy.mapToSource(index).row()

    def _current_item(self) -> BomItem | None:
        return self.model.item_at(self._current_source_row())

    def _selection_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        self._refresh_detail()
        self._update_actions()

    def _refresh_detail(self) -> None:
        row = self._current_source_row()
        self.detail.show_item(self.model.item_at(row), self.model.quote_at(row))

    def select_item(self, item: BomItem) -> None:
        row = self.model.row_of(item)
        if row < 0:
            return
        proxy_index = self.proxy.mapFromSource(self.model.index(row, 0))
        if proxy_index.isValid():
            self.table.setCurrentIndex(proxy_index)
            self.table.scrollTo(proxy_index)

    def _filter_changed(self, *args) -> None:
        self.proxy.set_text(self.filter_edit.text())
        self.proxy.set_levels(STATUS_FILTERS[self.status_filter.currentIndex()][1])
        self._refresh_filter()

    # ------------------------------------------------------------------ BOM

    def open_bom(self, path: str | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Abrir BOM", self.settings.last_dir, BOM_FILTER)
            if not path:
                return False
        try:
            table = load_table(path)
        except BomError as exc:
            QMessageBox.warning(self, "No se pudo leer el BOM", str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error al abrir el BOM", f"{type(exc).__name__}: {exc}")
            return False
        dialog = ImportDialog(table, self)
        if dialog.exec() != ImportDialog.Accepted:
            return False
        return self.load_table(dialog.table)

    def load_table(self, table: BomTable) -> bool:
        items, warnings = build_items(table)
        if not items:
            QMessageBox.warning(self, "BOM vacío", "No se encontraron partes en el archivo.")
            return False
        self.cancel_lookup()
        self._items_generation += 1
        self.table_doc = table
        self.items = items
        self.history_entry = None
        self._compare_baseline = None
        self.last_query = None
        self._pending_requery.clear()
        self.model.set_items(self.items, [])
        self.recalculate()
        self.setWindowTitle(f"{Path(table.path).name} – {APP_TITLE}")
        self.settings.last_dir = str(Path(table.path).parent)
        self.save_settings()
        if self.proxy.rowCount():
            self.table.setCurrentIndex(self.proxy.index(0, 0))
        self._update_query_age()
        message = f"BOM cargado: {len(items)} partes distintas."
        if warnings:
            message += " " + " ".join(warnings)
        self.statusBar().showMessage(message, 10000)
        if not self.settings.effective_api_key:
            self._show_key_banner()
        elif self.settings.auto_query_on_load:
            self.start_lookup()
        return True

    # ------------------------------------------------------------------ consultas

    def start_lookup(self, items: list[BomItem] | None = None, silent: bool = False) -> bool:
        targets = list(items) if items is not None else list(self.items)
        if not targets:
            return False
        if self._lookup_worker is not None:
            for item in targets:
                if item not in self._pending_requery:
                    self._pending_requery.append(item)
            return False
        if not self.settings.effective_api_key:
            self._show_key_banner()
            if not silent and self.interactive:
                self.open_settings()
            return False
        queries = queries_for(targets)
        if self.settings.passives_enabled:
            specs, required = specs_for(targets, self._read_params())
        else:
            mark_specs_disabled(targets)
            specs, required = [], {}
        if not queries and not specs:
            self.recalculate()
            return False
        client = self.client()
        defaults = Defaults(self.settings.res_tolerance_default, self.settings.cap_tolerance_default,
                            self.settings.cap_voltage_default)
        worker = Worker(lookup_all, client, queries, specs, defaults, required,
                        batch_size=self.settings.batch_size, fuzzy=self.settings.fuzzy_search,
                        with_progress=True, with_cancel=True)
        worker.signals.progress.connect(self._lookup_progress)
        worker.signals.result.connect(self._lookup_done)
        worker.signals.error.connect(self._lookup_failed)
        worker.signals.finished.connect(self._lookup_finished)
        self._lookup_worker = worker
        self._lookup_targets = targets
        self._lookup_generation = self._items_generation
        self._lookup_started = time.monotonic()
        self._lookup_silent = silent
        self.progress.setRange(0, max(1, len(set(queries)) + len({spec.key for spec in specs})))
        self.progress.setValue(0)
        self.progress.setFormat("Consultando… %v/%m")
        self.progress.show()
        self.cancel_button.show()
        self._set_connection("busy", "Consultando Mouser…")
        self._update_actions()
        self.workers.start(worker)
        return True

    @Slot()
    def cancel_lookup(self) -> None:
        if self._lookup_worker is not None:
            self._lookup_worker.cancel()
            self._pending_requery.clear()

    @Slot()
    def _user_cancel(self) -> None:
        """Botón «Cancelar»: detiene la consulta (y la comparación con el historial, si estaba en curso)."""
        self._compare_baseline = None
        self.cancel_lookup()

    @Slot(int, int, str)
    def _lookup_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(done, total))
        self.statusBar().showMessage(message)
        self._update_usage()

    @Slot(object)
    def _lookup_done(self, bundle) -> None:
        if self._lookup_generation != self._items_generation:
            return  # la consulta era de un BOM que ya se reemplazó: sus resultados no aplican
        now = datetime.now()
        apply_lookup(self._lookup_targets, bundle.parts, now, spec_results=bundle.specs)
        full = len(self._lookup_targets) == len(self.items)
        if full or self.last_query is None:
            self.last_query = now
        self.recalculate()
        seconds = time.monotonic() - self._lookup_started
        detail = f"{len(bundle.parts)} números de parte"
        if bundle.specs:
            detail += f" y {len(bundle.specs)} pasivos por especificación"
        self.statusBar().showMessage(f"Precios y stock actualizados: {detail} en {seconds:.0f} s.", 10000)
        self._set_connection("ok", "Conectado a Mouser")
        self._hide_banner()
        self._update_query_age()
        if full and self._compare_baseline is not None:
            self._finish_comparison()

    @Slot(object)
    def _lookup_failed(self, exc: Exception) -> None:
        if self._lookup_generation != self._items_generation:
            return  # error de una consulta de un BOM que ya se reemplazó
        if isinstance(exc, MouserCancelled):
            self.statusBar().showMessage("Consulta cancelada.", 8000)
            self._set_connection("idle", "Consulta cancelada")
            return
        if self._compare_baseline is not None:
            self._compare_baseline = None  # sin precios actuales no hay comparación
            self.statusBar().showMessage("No se pudo comparar con los precios actuales.", 10000)
        if isinstance(exc, MouserAuthError):
            self._set_connection("error", "API key rechazada")
            self._show_banner(f"{exc} <a href='settings'>Abrir configuración</a>", error=True)
        elif isinstance(exc, MouserConnectionError):
            self._set_connection("error", "Sin conexión con Mouser")
            extra = (" Se muestran los últimos precios obtenidos." if self.last_query else "")
            self._show_banner(f"{exc}{extra} <a href='retry'>Reintentar</a>", error=True)
        elif isinstance(exc, MouserRateLimitError):
            self._set_connection("error", "Límite de consultas")
            self._show_banner(str(exc), error=True)
        else:
            self._set_connection("error", "Error en la consulta")
            self._show_banner(f"Error al consultar Mouser: {exc}", error=True)
        if not self._lookup_silent:
            self.statusBar().showMessage(str(exc), 15000)

    @Slot()
    def _lookup_finished(self) -> None:
        self._lookup_worker = None
        self.progress.hide()
        self.cancel_button.hide()
        self._update_actions()
        self._update_usage()
        self.save_settings()
        if self._pending_requery:
            pending = [i for i in self._pending_requery if i in self.items]
            self._pending_requery = []
            if pending:
                self.start_lookup(pending, silent=True)

    def _auto_refresh(self) -> None:
        if (self.items and self._lookup_worker is None and self._cart_worker is None
                and self.settings.effective_api_key):
            self.start_lookup(silent=True)

    def _apply_auto_refresh(self) -> None:
        minutes = self.settings.auto_refresh_minutes
        if minutes > 0:
            self.auto_timer.start(minutes * 60_000)
        else:
            self.auto_timer.stop()

    def _auto_spin_changed(self, value: int) -> None:
        self.settings.auto_refresh_minutes = value
        self._apply_auto_refresh()
        self._update_query_age()

    def _startup(self) -> None:
        if not self.settings.effective_api_key:
            self._set_connection("idle", "Sin API key configurada")
            self._show_key_banner()
            return
        self._set_connection("busy", "Verificando conexión…")
        worker = Worker(self.client().test_connection)
        worker.signals.result.connect(self._startup_ok)
        worker.signals.error.connect(self._startup_failed)
        worker.signals.finished.connect(self._update_usage)
        self.workers.start(worker)

    @Slot(object)
    def _startup_ok(self, message: str) -> None:
        if self._lookup_worker is None:
            self._set_connection("ok", message)

    @Slot(object)
    def _startup_failed(self, exc: Exception) -> None:
        if isinstance(exc, MouserAuthError):
            self._set_connection("error", "API key rechazada")
            self._show_banner(f"{exc} <a href='settings'>Abrir configuración</a>", error=True)
        else:
            self._set_connection("error", "Sin conexión con Mouser")
            self._show_banner(f"{exc} <a href='retry-connection'>Reintentar</a>", error=True)

    # ------------------------------------------------------------------ estado visual

    def _set_connection(self, state: str, text: str) -> None:
        self._connection_state = (state, text)
        color = {"ok": "#1A7F37", "busy": "#1F5FAD", "error": "#CF222E"}.get(state, "#6E7781")
        html = f"<span style='color:{color}'>●</span> {text}"
        self.connection_label.setText(html)
        self.status_connection.setText(html)

    def _show_banner(self, html: str, error: bool = False) -> None:
        self.banner.setObjectName("ErrorBanner" if error else "Banner")
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setText(html)
        self.banner.show()

    def _hide_banner(self) -> None:
        self.banner.hide()

    def _show_key_banner(self) -> None:
        self._show_banner("Para consultar precios y stock en tiempo real ingrese su API key de la "
                          "<b>Mouser Search API</b>. <a href='settings'>Configurar ahora</a>")

    def _banner_link(self, link: str) -> None:
        if link == "settings":
            self.open_settings()
        elif link == "retry":
            self._hide_banner()
            self.start_lookup()
        elif link == "retry-connection":
            self._hide_banner()
            self._startup()

    def _update_query_age(self) -> None:
        if self.last_query is None:
            text = "Sin consultas todavía" if self.items else "Abra un BOM para cotizar"
        else:
            minutes = int((datetime.now() - self.last_query).total_seconds() // 60)
            if minutes < 1:
                text = f"Precios consultados recién ({self.last_query.strftime('%H:%M')})"
            elif minutes < 60:
                text = f"Precios consultados hace {minutes} min ({self.last_query.strftime('%H:%M')})"
            elif minutes < 24 * 60:
                text = f"Precios consultados hace {minutes // 60} h ({self.last_query.strftime('%H:%M')})"
            else:
                text = f"Precios del {self.last_query.strftime('%d-%m-%Y %H:%M')}"
        if self.settings.auto_refresh_minutes and self.items:
            text += f" · se actualizan cada {self.settings.auto_refresh_minutes} min"
        self.query_age_label.setText(text)

    def _update_usage(self) -> None:
        used = self.settings.calls_today
        color = "#CF222E" if used >= DAILY_LIMIT * 0.9 else "#59636E"
        self.usage_label.setText(
            f"<span style='color:{color}'>Consultas a la API hoy: {fmt_int(used)} de {fmt_int(DAILY_LIMIT)}</span>")

    # ------------------------------------------------------------------ edición de partes

    @Slot(object, str)
    def _on_item_edited(self, item: BomItem, field: str) -> None:
        if field in ("mpn", "mouser_pn"):
            item.manual_part = None
            item.candidates, item.near, item.suggestions = [], [], []
            item.lookup_error = ""
            item.spec_report = None
            if not item.base_query:
                item.spec = parse_spec(item.designators, item.value, item.description, item.footprint, item.extra)
            if item.base_query or (item.by_spec and item.spec.complete):
                item.lookup_state = "pending"
            else:
                item.lookup_state = "noquery"
            self.recalculate()
            if item.lookup_state == "pending" and self.settings.effective_api_key:
                self.start_lookup([item], silent=True)
            return
        self.schedule_recalc()

    def _use_option(self, part: Part) -> None:
        item = self._current_item()
        if item is None:
            return
        item.manual_part = part
        self.recalculate()
        self.select_item(item)

    def _auto_select(self) -> None:
        item = self._current_item()
        if item is None:
            return
        item.manual_part = None
        self.recalculate()
        self.select_item(item)

    def _requery_selected(self) -> None:
        item = self._current_item()
        if item is not None:
            self.start_lookup([item])

    def search_selected(self) -> None:
        item = self._current_item()
        if item is None:
            return
        if not self.settings.effective_api_key:
            self._show_key_banner()
            self.open_settings()
            return
        row = self._current_source_row()
        quote = self.model.quote_at(row)
        dialog = SearchDialog(self.client(), self.workers, item, quote.required if quote else 1,
                              keyword_for(item), self)
        if dialog.exec() and dialog.selected_part is not None:
            item.manual_part = dialog.selected_part
            self.recalculate()
            self.select_item(item)
        self._update_usage()

    def _table_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        self.table.setCurrentIndex(index)
        item = self._current_item()
        quote = self.model.quote_at(self._current_source_row())
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction(self.act_search)
        requery = menu.addAction("Volver a consultar esta parte")
        requery.setEnabled(bool(item.base_query or item.manual_part))
        auto = menu.addAction("Volver a selección automática")
        auto.setEnabled(item.manual_part is not None)
        menu.addSeparator()
        toggle = menu.addAction("Excluir de la compra" if item.include else "Incluir en la compra")
        menu.addSeparator()
        open_part = menu.addAction("Ver en Mouser")
        open_part.setEnabled(bool(quote and quote.part and quote.part.product_url))
        datasheet = menu.addAction("Abrir datasheet")
        datasheet.setEnabled(bool(quote and quote.part and quote.part.datasheet_url))
        copy_pn = menu.addAction("Copiar N° Mouser")
        copy_pn.setEnabled(bool(quote and quote.part))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is requery:
            self.start_lookup([item])
        elif chosen is auto:
            self._auto_select()
        elif chosen is toggle:
            item.include = not item.include
            self.recalculate()
        elif chosen is open_part and quote and quote.part:
            QDesktopServices.openUrl(QUrl(quote.part.product_url))
        elif chosen is datasheet and quote and quote.part:
            QDesktopServices.openUrl(QUrl(quote.part.datasheet_url))
        elif chosen is copy_pn and quote and quote.part:
            QApplication.clipboard().setText(quote.part.mouser_pn)

    # ------------------------------------------------------------------ exportación

    def _default_name(self, prefix: str, suffix: str) -> str:
        stem = Path(self.table_doc.path).stem if self.table_doc else "BOM"
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        return str(Path(self.settings.last_dir or Path.home()) / f"{prefix}_{stem}_{stamp}{suffix}")

    def export_excel(self, path: str | None = None) -> str | None:
        if not self.items:
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Exportar cotización a Excel",
                                                  self._default_name("Cotizacion_Mouser", ".xlsx"),
                                                  "Excel (*.xlsx)")
            if not path:
                return None
        written = self._write_excel(path, self.snapshot())
        if written:
            self.save_to_history("excel", quiet=True)
        return written

    def _write_excel(self, path: str, snapshot: Snapshot) -> str | None:
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        quotes = quote_all(snapshot.items, snapshot.params)
        summary = summarize(snapshot.items, quotes, snapshot.params)
        try:
            export_excel(path, snapshot.items, quotes, summary, snapshot.params,
                         bom_path=snapshot.bom_path, bom_grid=snapshot.bom_grid, queried_at=snapshot.queried_at,
                         scenario_quantities=self.scenarios.quantities, scenario_max=self.scenarios.max_boards,
                         client_name=snapshot.client, company_name=self.settings.company_name)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo guardar", f"{exc}\n\n¿Está abierto el archivo en Excel?")
            return None
        self._offer_open(path, "Cotización exportada")
        return path

    def export_cart(self, path: str | None = None) -> str | None:
        if not self.items:
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Exportar carro Mouser (CSV)",
                                                  self._default_name("Carro_Mouser", ".csv"), "CSV (*.csv)")
            if not path:
                return None
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            count = export_cart_csv(path, self.items, self.quotes)
        except OSError as exc:
            QMessageBox.warning(self, "No se pudo guardar", str(exc))
            return None
        self.statusBar().showMessage(f"Carro exportado: {count} líneas con código Mouser y cantidad.", 10000)
        self._offer_open(path, "Carro exportado",
                         f"{count} líneas listas para cargar en mouser.com (herramienta de carga de "
                         "BOM / Excel al carro).")
        return path

    # ------------------------------------------------------------------ carro en Mouser

    def _cart_notes(self, lines: list[CartLine]) -> list[str]:
        """Advertencias antes de crear el carro: partes que no van y partes sin stock suficiente."""
        in_cart = {item_id for line in lines for item_id in line.item_ids}
        left_out = [item for item, q in zip(self.items, self.quotes)
                    if item.include and q.level != LEVEL_EXCLUDED and q.required and item.id not in in_cart]
        notes = []
        if left_out:
            names = ", ".join(i.mpn or i.designators or f"línea {i.rows_label}" for i in left_out[:6])
            more = "…" if len(left_out) > 6 else ""
            notes.append(f"{len(left_out)} {'parte queda' if len(left_out) == 1 else 'partes quedan'} fuera del "
                         f"carro (sin precio o sin código Mouser): {names}{more}.")
        short = [q for item, q in zip(self.items, self.quotes)
                 if item.id in in_cart and q.part is not None and q.part.stock is not None
                 and q.part.stock < q.buy_qty]
        if short:
            notes.append(f"{len(short)} {'ítem supera' if len(short) == 1 else 'ítems superan'} el stock actual de "
                         "Mouser: la diferencia quedará como pedido pendiente (backorder).")
        return notes

    def create_cart(self) -> bool:
        """Crea un carro NUEVO en Mouser con la cotización actual. Nunca envía un pedido."""
        if self._cart_worker is not None or not self.items:
            return False
        lines = cart_lines(self.items, self.quotes)
        if not lines:
            QMessageBox.information(self, "Crear carro en Mouser",
                                    "No hay partes con precio y código Mouser para agregar al carro.")
            return False
        if not self.settings.effective_cart_api_key:
            if self.interactive:
                answer = QMessageBox.question(
                    self, "Crear carro en Mouser",
                    "Para crear el carro se necesita la clave de la Cart API (My Mouser → APIs → "
                    "Cart/Order API), distinta de la clave de la Search API.\n\n¿Abrir la configuración?")
                if answer == QMessageBox.Yes:
                    self.open_settings()
            return False
        if len(lines) > MAX_CART_ITEMS:
            QMessageBox.warning(self, "Crear carro en Mouser",
                                f"Un carro de Mouser admite hasta {MAX_CART_ITEMS} ítems y esta cotización tiene "
                                f"{len(lines)}. Divida el BOM o use «Exportar carro Mouser».")
            return False
        if self.interactive:
            dialog = CartConfirmDialog(lines, self.summary.currency, self._cart_notes(lines), self)
            if dialog.exec() != QDialog.Accepted:
                return False
        worker = Worker(self.cart_client().cart_insert, lines, with_progress=True, with_cancel=True)
        worker.signals.progress.connect(self._cart_progress)
        worker.signals.result.connect(self._cart_done)
        worker.signals.error.connect(self._cart_failed)
        worker.signals.finished.connect(self._cart_finished)
        self._cart_worker = worker
        self._cart_lines = lines
        self._cart_snapshot = self.snapshot()
        self.progress.setRange(0, 0)
        self.progress.setFormat("Creando el carro…")
        self.progress.show()
        self.statusBar().showMessage("Creando el carro en Mouser…")
        self._update_actions()
        self.workers.start(worker)
        return True

    @Slot(int, int, str)
    def _cart_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(min(done, total))
        self.statusBar().showMessage(message)

    @Slot(object)
    def _cart_done(self, result: CartResult) -> None:
        self.last_cart = result
        if result.cart_key:
            self.statusBar().showMessage(
                f"Carro creado en Mouser ({len(result.items)} ítems, {fmt_money(result.total, result.currency)}). "
                f"Clave: {result.cart_key}", 20000)
        else:
            self.statusBar().showMessage("Mouser no creó el carro: " + ("; ".join(result.errors) or "sin detalle"),
                                         20000)
        if result.cart_key:  # se guarda la cotización tal como estaba al crear el carro
            self.save_to_history(reason="cart", cart_key=result.cart_key, quiet=True, snapshot=self._cart_snapshot)
        if self.interactive and self.isVisible():
            known = [line.ext_price for line in self._cart_lines if line.ext_price is not None]
            local_total = sum(known, Decimal("0")) if len(known) == len(self._cart_lines) else None
            CartResultDialog(result, local_total, self).exec()

    @Slot(object)
    def _cart_failed(self, exc: Exception) -> None:
        if isinstance(exc, MouserCancelled):
            self.statusBar().showMessage("Creación del carro cancelada.", 8000)
            return
        text = str(exc)
        if isinstance(exc, MouserConnectionError) and "proxy" not in text.lower():
            text += ("\n\nNo se pudo confirmar si Mouser alcanzó a crear el carro. Revise mouser.com antes de "
                     "intentarlo otra vez para no duplicarlo.")
        self.statusBar().showMessage(f"No se pudo crear el carro: {exc}", 15000)
        if self.interactive and self.isVisible():
            QMessageBox.warning(self, "No se pudo crear el carro", text)

    @Slot()
    def _cart_finished(self) -> None:
        self._cart_worker = None
        self.progress.hide()
        self._update_actions()

    # ------------------------------------------------------------------ historial

    def snapshot(self) -> Snapshot:
        """La cotización actual, lista para guardar en el historial."""
        doc = self.table_doc
        return Snapshot(
            items=self.items, params=self._read_params(), bom_path=doc.path if doc else "",
            client=self.client_edit.text().strip(), queried_at=self.last_query,
            bom_grid=doc.grid if doc else None, bom_sheet=doc.sheet if doc else "",
            bom_header_row=doc.header_row if doc else 0, bom_mapping=dict(doc.mapping) if doc else {})

    def save_to_history(self, reason: str = "manual", cart_key: str = "", quiet: bool = False,
                        snapshot: Snapshot | None = None) -> int | None:
        """Guarda la cotización (la actual o `snapshot`) en el historial local. Al exportar o crear el carro
        se llama con quiet=True."""
        snapshot = snapshot or (self.snapshot() if self.items else None)
        if snapshot is None or not snapshot.items:
            return None
        try:
            entry_id = self.history.save(snapshot, reason, cart_key)
        except HistoryError as exc:
            self.statusBar().showMessage(str(exc), 15000)
            if not quiet and self.interactive:
                QMessageBox.warning(self, "Historial", str(exc))
            return None
        if not quiet:
            self.statusBar().showMessage(f"Cotización guardada en el historial (N° {entry_id}).", 8000)
        self.detail.refresh_history()
        return entry_id

    def open_history(self) -> None:
        dialog = HistoryDialog(self.history, self)
        if dialog.exec() != QDialog.Accepted or dialog.selected is None:
            return
        if dialog.action == "export":
            self.export_history_entry(dialog.selected.id)
        else:
            self.open_history_entry(dialog.selected.id, compare=dialog.action == "compare")

    def _load_history(self, entry_id: int) -> tuple[Snapshot, HistoryEntry] | None:
        try:
            snapshot = self.history.load(entry_id)
            entry = self.history.entry(entry_id)
        except HistoryError as exc:
            QMessageBox.warning(self, "Historial", str(exc))
            return None
        if entry is None:
            return None
        return snapshot, entry

    def open_history_entry(self, entry_id: int, compare: bool = False) -> bool:
        """Abre una cotización guardada con sus precios; con `compare`, consulta Mouser y muestra qué cambió."""
        loaded = self._load_history(entry_id)
        if loaded is None:
            return False
        snapshot, entry = loaded
        self.load_snapshot(snapshot, entry)
        if compare:
            self._compare_baseline = {item.id: q for item, q in zip(self.items, self.quotes)}
            self._compare_before_total = self.summary.total
            self._hide_banner()
            self.statusBar().showMessage("Consultando los precios actuales para comparar…")
            if not self.start_lookup() and self._lookup_worker is None:
                self._compare_baseline = None
        return True

    def load_snapshot(self, snapshot: Snapshot, entry: HistoryEntry | None = None) -> None:
        self.cancel_lookup()
        self._items_generation += 1
        self._pending_requery.clear()
        self._compare_baseline = None
        name = snapshot.bom_name or "Cotización"
        if snapshot.bom_grid is not None or snapshot.bom_path:
            self.table_doc = BomTable(path=snapshot.bom_path or name, sheet_names=[snapshot.bom_sheet],
                                      sheet=snapshot.bom_sheet, grid=snapshot.bom_grid or [],
                                      header_row=snapshot.bom_header_row, mapping=dict(snapshot.bom_mapping))
        else:
            self.table_doc = None
        self.items = snapshot.items
        self.history_entry = entry
        self.last_query = snapshot.queried_at
        self.client_edit.setText(snapshot.client)
        self._apply_params(snapshot.params)
        self.model.set_items(self.items, [])
        self.recalculate()
        saved = fmt_when(entry.created_at) if entry else ""
        self.setWindowTitle(f"{name} · historial {saved} – {APP_TITLE}".replace(" ·  –", " –"))
        if self.proxy.rowCount():
            self.table.setCurrentIndex(self.proxy.index(0, 0))
        self._update_query_age()
        if entry is not None:
            self._show_banner(
                f"Cotización del historial guardada el {saved} ({entry.reason_label.lower()}): se muestran los "
                f"precios y el stock de ese momento. <a href='retry'>Actualizar precios</a>")

    def export_history_entry(self, entry_id: int, path: str | None = None) -> str | None:
        """Exporta a Excel una cotización guardada, sin reemplazar la que está abierta."""
        loaded = self._load_history(entry_id)
        if loaded is None:
            return None
        snapshot, entry = loaded
        if not path:
            stem = Path(snapshot.bom_name).stem or "BOM"
            suggested = Path(self.settings.last_dir or Path.home()) / (
                f"Cotizacion_Mouser_{stem}_{entry.created_at.strftime('%Y%m%d_%H%M')}.xlsx")
            path, _ = QFileDialog.getSaveFileName(self, "Exportar cotización del historial", str(suggested),
                                                  "Excel (*.xlsx)")
            if not path:
                return None
        return self._write_excel(path, snapshot)

    def _part_history(self, part: Part | None, item: BomItem | None) -> list[PriceRecord]:
        mouser_pn = part.mouser_pn if part else (item.mouser_pn if item else "")
        mpn = part.mpn if part else (item.mpn if item else "")
        try:
            return self.history.part_history(mouser_pn=mouser_pn, mpn=mpn)
        except HistoryError:
            return []

    def _finish_comparison(self) -> None:
        baseline = self._compare_baseline
        self._compare_baseline = None
        if baseline is None:
            return
        rows = compare_quotes(self.items, baseline, self.quotes)
        self.last_comparison = rows
        changed = sum(1 for r in rows if r.status != "Sin cambio")
        self.statusBar().showMessage(f"Comparación lista: {changed} de {len(rows)} partes cambiaron.", 15000)
        if self.interactive and self.isVisible():
            ComparisonDialog(rows, self._compare_before_total, self.summary.total, self.summary.currency,
                             self.history_entry.created_at if self.history_entry else None, self).exec()

    def _offer_open(self, path: str, title: str, text: str = "") -> None:
        if not self.interactive or not self.isVisible():
            return
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Information)
        box.setText(f"{text}\n\n{path}".strip())
        open_button = box.addButton("Abrir archivo", QMessageBox.AcceptRole)
        folder_button = box.addButton("Abrir carpeta", QMessageBox.ActionRole)
        box.addButton("Cerrar", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif box.clickedButton() is folder_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    # ------------------------------------------------------------------ otros

    def fetch_fx(self) -> None:
        currency = self.summary.currency or "USD"
        self.fx_button.setEnabled(False)
        worker = Worker(fetch_clp_rate, currency)
        worker.signals.result.connect(self._fx_ok)
        worker.signals.error.connect(self._fx_failed)
        worker.signals.finished.connect(lambda: self.fx_button.setEnabled(True))
        self.workers.start(worker)

    @Slot(object)
    def _fx_ok(self, result) -> None:
        rate, day = result
        self.fx_spin.setValue(rate)
        self.statusBar().showMessage(f"Tipo de cambio observado {day}: {fmt_num(rate, 2)} CLP.", 10000)

    @Slot(object)
    def _fx_failed(self, exc: Exception) -> None:
        QMessageBox.information(self, "Tipo de cambio", f"{exc}\n\nPuede ingresarlo manualmente.")

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.client, self.workers, self)
        had_key = bool(self.settings.effective_api_key)
        if dialog.exec():
            dialog.apply()
            self.auto_spin.blockSignals(True)
            self.auto_spin.setValue(self.settings.auto_refresh_minutes)
            self.auto_spin.blockSignals(False)
            self._apply_auto_refresh()
            self.save_settings()
            self._update_query_age()
            if self.settings.effective_api_key:
                self._hide_banner()
                self._startup()
                if not had_key and self.items and self.last_query is None:
                    self.start_lookup()
            else:
                self._show_key_banner()
        self._update_usage()

    def show_help(self) -> None:
        QMessageBox.information(self, "Cómo usar MouserEngine", HELP_TEXT)

    def show_about(self) -> None:
        QMessageBox.about(
            self, "Acerca de MouserEngine",
            f"<b>MouserEngine {__version__}</b><br>Cotizador de BOM con precios y stock en tiempo real "
            "desde la Mouser Search API.<br><br>Los precios y el stock provienen de Mouser al momento de "
            "cada consulta y pueden cambiar.")

    def create_desktop_shortcut(self) -> None:
        try:
            path = create_windows_shortcut()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Acceso directo", f"No se pudo crear el acceso directo: {exc}")
            return
        QMessageBox.information(self, "Acceso directo", f"Acceso directo creado en:\n{path}")

    # ------------------------------------------------------------------ ventana

    def dragEnterEvent(self, event) -> None:
        if any(Path(u.toLocalFile()).suffix.lower() in BOM_SUFFIXES for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if Path(path).suffix.lower() in BOM_SUFFIXES:
                event.acceptProposedAction()
                QTimer.singleShot(0, lambda p=path: self.open_bom(p))
                return

    def _header_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addSection("Columnas visibles")
        for index, (key, title, _) in enumerate(COLUMNS):
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(index))
            action.setEnabled(key not in ("include", "mpn"))
            action.toggled.connect(lambda on, i=index: self.table.setColumnHidden(i, not on))
        menu.addSeparator()
        reset = menu.addAction("Restablecer columnas")
        reset.triggered.connect(self._reset_columns)
        menu.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _reset_columns(self) -> None:
        header = self.table.horizontalHeader()
        for index, (_, _, width) in enumerate(COLUMNS):
            self.table.setColumnHidden(index, False)
            header.moveSection(header.visualIndex(index), index)
            self.table.setColumnWidth(index, width)

    def _restore_geometry(self) -> None:
        self._geometry_restored = False
        if not self.persist:
            return
        store = QSettings(APP_NAME, APP_NAME)
        geometry = store.value("geometry")
        if geometry is not None:
            self._geometry_restored = bool(self.restoreGeometry(geometry))
        header_state = store.value("table_header")
        if header_state is not None:
            self.table.horizontalHeader().restoreState(header_state)
        splitter_state = store.value("splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)

    def show_initial(self) -> None:
        """Primera ejecución: maximizada; después, como se dejó."""
        if self._geometry_restored:
            self.show()
        else:
            self.showMaximized()

    def closeEvent(self, event) -> None:
        self.cancel_lookup()
        self.workers.cancel_all()
        self.save_settings()
        if self.persist:
            store = QSettings(APP_NAME, APP_NAME)
            store.setValue("geometry", self.saveGeometry())
            store.setValue("table_header", self.table.horizontalHeader().saveState())
            store.setValue("splitter", self.splitter.saveState())
        self.workers.wait(3000)
        super().closeEvent(event)


HELP_TEXT = """<b>1. Configure su API key</b> (Herramientas → Configuración). Debe ser la clave de la
<b>Mouser Search API</b>, disponible en My Mouser → APIs.<br><br>
<b>2. Abra el BOM</b> (Excel o CSV) o arrástrelo a la ventana. Revise las columnas detectadas y presione
«Importar». Las líneas con el mismo número de parte se agrupan en una sola compra.<br><br>
<b>3. Ajuste la cantidad de placas y la merma.</b> Los totales se recalculan al instante con los tramos de
precio de Mouser (mínimo de compra y múltiplo incluidos).<br><br>
<b>4. Revise las partes con advertencias o problemas</b> (sin stock, obsoletas, no encontradas…). En la
pestaña «Opciones en Mouser» puede elegir otra presentación, y con «Buscar en Mouser» puede asignar una
parte a líneas sin número de parte o no encontradas.<br><br>
<b>5. Escenarios de volumen:</b> en la pestaña del mismo nombre, mueva la barra de cantidad para ver el
costo por placa y el costo total de 1 a 1.000 placas (o el máximo que elija), y compare las cantidades de
la tabla. Las bajas de 10 % o más aparecen en verde.<br><br>
<b>6. Resistencias y condensadores sin MPN:</b> se eligen solos según valor, encapsulado, tolerancia,
potencia, tensión y dieléctrico: la opción más conveniente que cumple o supera lo pedido, de un fabricante
reconocido. Puede cambiarla en «Opciones en Mouser».<br><br>
<b>7. Exporte</b> la cotización a Excel, el carro en CSV, o use <b>«Crear carro en Mouser»</b> para dejar el
carro armado en su cuenta (necesita la clave de Cart API). La aplicación nunca envía pedidos.<br><br>
<b>8. Historial:</b> Ctrl+S guarda la cotización en este computador (también se guarda al exportar a Excel
y al crear el carro). En «Historial» puede reabrirla, compararla con los precios de hoy o exportarla.<br><br>
Presione <b>F5</b> para volver a consultar precios y stock. Puede activar la actualización automática en
el panel izquierdo. Mouser permite hasta 1.000 consultas diarias por API key; cada consulta cubre hasta
10 números de parte."""


def create_windows_shortcut() -> str:
    """Crea un acceso directo en el escritorio de Windows apuntando a la aplicación."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("Solo disponible en Windows.")
    if getattr(sys, "frozen", False):
        target, arguments = sys.executable, ""
        workdir = str(Path(sys.executable).parent)
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        target = str(pythonw if pythonw.exists() else sys.executable)
        arguments = "-m mouser_engine"
        workdir = str(Path(__file__).resolve().parents[2])

    def ps(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    script = (
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        "$path = Join-Path $desktop 'MouserEngine.lnk'; "
        "$shell = New-Object -ComObject WScript.Shell; "
        "$link = $shell.CreateShortcut($path); "
        f"$link.TargetPath = {ps(target)}; "
        f"$link.Arguments = {ps(arguments)}; "
        f"$link.WorkingDirectory = {ps(workdir)}; "
        f"$link.IconLocation = {ps(target)}; "
        "$link.Description = 'Cotizador de BOM Mouser'; "
        "$link.Save(); Write-Output $path"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell devolvió un error")
    return result.stdout.strip()
