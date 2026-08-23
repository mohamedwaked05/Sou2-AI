import {
  ArrowLeft,
  ArrowRight,
  Building2,
  Check,
  LogOut,
  MapPin,
  Plus,
  Store,
  Trash2,
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, Business, User, WorkingDay } from "./api";
import { CATEGORIES, categoryLabel, emptySchedule, LOCATIONS } from "./constants";
import { Alert, BusyLabel, Logo, Skeleton, StatusBadge, ThemeButton } from "./ui";

function errorMessage(error: unknown) {
  return error instanceof ApiError
    ? error.message
    : "We couldn't complete that request. Try again.";
}

export function BusinessPicker({
  user,
  dark,
  setDark,
  onLogout,
}: {
  user: User;
  dark: boolean;
  setDark: (value: boolean) => void;
  onLogout: () => Promise<void>;
}) {
  const navigate = useNavigate();
  const [businesses, setBusinesses] = useState<Business[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    api
      .businesses()
      .then((items) => current && setBusinesses(items))
      .catch((caught) => current && setError(errorMessage(caught)))
      .finally(() => current && setLoading(false));
    return () => {
      current = false;
    };
  }, []);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) return;
    setCreating(true);
    setError("");
    try {
      const business = await api.createBusiness(name.trim());
      navigate(`/businesses/${business.id}/onboarding`);
    } catch (caught) {
      setError(errorMessage(caught));
      setCreating(false);
    }
  }

  return (
    <main className="picker-page">
      <div className="picker-wave" aria-hidden="true" />
      <header className="picker-header">
        <Logo />
        <div className="flex items-center gap-2">
          <ThemeButton dark={dark} onChange={() => setDark(!dark)} />
          <Link
            className="account-chip"
            to="/account"
            aria-label="Open account settings"
          >
            <span>
              {user.first_name.slice(0, 1)}
              {user.last_name.slice(0, 1)}
            </span>
            <span className="hidden sm:block">{user.first_name}</span>
          </Link>
          <button
            type="button"
            className="icon-button"
            aria-label="Sign out"
            onClick={() => void onLogout()}
          >
            <LogOut size={18} />
          </button>
        </div>
      </header>
      <section className="picker-content">
        <div className="mb-8">
          <p className="eyebrow">Your workspace</p>
          <h1>Select your business</h1>
          <p>Choose a business to access its assistant and data.</p>
        </div>
        {error && (
          <div className="mb-5">
            <Alert>{error}</Alert>
          </div>
        )}
        {loading ? (
          <div className="business-grid" aria-label="Loading businesses">
            <Skeleton />
            <Skeleton />
            <Skeleton />
          </div>
        ) : (
          <div className="business-grid">
            {businesses.map((business) => {
              const canOpen =
                business.status === "ACTIVE" ||
                (business.status === "PENDING" && !business.onboarding_submitted_at);
              return (
                <article
                  key={business.id}
                  className={`business-card ${!canOpen ? "business-card-disabled" : ""}`}
                >
                  <div className="flex items-start justify-between gap-4">
                    <span className="business-icon">
                      <Store />
                    </span>
                    <StatusBadge status={business.status} />
                  </div>
                  <div className="mt-5">
                    <h2>{business.name}</h2>
                    <p>{categoryLabel(business.category)}</p>
                  </div>
                  <div className="business-location">
                    <MapPin size={15} />
                    {business.city && business.governorate
                      ? `${business.city}, ${business.governorate}`
                      : "Location not set"}
                  </div>
                  <div className="business-completion">
                    <span>Profile</span>
                    <strong>
                      {business.profile_complete ? "Complete" : "Incomplete"}
                    </strong>
                  </div>
                  <div className="completion-track">
                    <span className={business.profile_complete ? "w-full" : "w-0"} />
                  </div>
                  <p className="mt-4 text-xs font-medium text-blue-600 dark:text-blue-400">
                    {business.status === "ACTIVE"
                      ? "Open workspace"
                      : business.status === "DISABLED"
                        ? "Workspace disabled"
                        : business.onboarding_submitted_at
                          ? "Pending review"
                          : "Continue setup"}
                  </p>
                  {canOpen && (
                    <Link
                      className="business-card-link"
                      to={
                        business.status === "ACTIVE"
                          ? `/businesses/${business.id}/overview`
                          : `/businesses/${business.id}/onboarding`
                      }
                      aria-label={`${business.status === "ACTIVE" ? "Open" : "Continue setup for"} ${business.name}`}
                    />
                  )}
                </article>
              );
            })}
            <form onSubmit={create} className="business-card create-business-card">
              <span className="business-icon">
                <Plus />
              </span>
              <div className="mt-5">
                <h2>Create new business</h2>
                <p>Start a separate tenant workspace.</p>
              </div>
              <label className="field-label mt-5">
                Business name
                <input
                  required
                  name="business_name"
                  minLength={2}
                  maxLength={120}
                  className="input"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <button
                type="submit"
                disabled={creating || name.trim().length < 2}
                className="btn mt-4 w-full"
              >
                <BusyLabel busy={creating} idle="Create business" />
              </button>
            </form>
          </div>
        )}
      </section>
    </main>
  );
}

type BusinessDraft = {
  name: string;
  category: string;
  custom_category: string;
  governorate: string;
  district: string;
  city: string;
  address_line: string;
  default_language: string;
  description: string;
  working_hours: WorkingDay[];
};

function toDraft(business: Business): BusinessDraft {
  return {
    name: business.name,
    category: business.category ?? "",
    custom_category: business.custom_category ?? "",
    governorate: business.governorate ?? "",
    district: business.district ?? "",
    city: business.city ?? "",
    address_line: business.address_line ?? "",
    default_language: business.default_language ?? "",
    description: business.description ?? "",
    working_hours:
      business.working_hours.length === 7
        ? business.working_hours.map((day) => ({
            ...day,
            shifts: day.shifts.map((shift) => ({
              start: shift.start.slice(0, 5),
              end: shift.end.slice(0, 5),
            })),
          }))
        : emptySchedule(),
  };
}

export function ScheduleEditor({
  value,
  onChange,
}: {
  value: WorkingDay[];
  onChange: (days: WorkingDay[]) => void;
}) {
  function updateDay(index: number, next: WorkingDay) {
    onChange(value.map((day, dayIndex) => (dayIndex === index ? next : day)));
  }
  return (
    <div className="schedule-list">
      {value.map((day, index) => (
        <fieldset key={day.weekday} className="schedule-day">
          <legend>{day.weekday.toLowerCase()}</legend>
          <label className="switch-label">
            <input
              name={`${day.weekday.toLowerCase()}_open`}
              type="checkbox"
              checked={!day.is_closed}
              onChange={(event) =>
                updateDay(index, {
                  ...day,
                  is_closed: !event.target.checked,
                  shifts: event.target.checked
                    ? day.shifts.length
                      ? day.shifts
                      : [{ start: "09:00", end: "17:00" }]
                    : [],
                })
              }
            />
            <span aria-hidden="true" />
            {day.is_closed ? "Closed" : "Open"}
          </label>
          {!day.is_closed && (
            <div className="schedule-shifts">
              {day.shifts.map((shift, shiftIndex) => (
                <div className="shift-row" key={`${day.weekday}-${shiftIndex}`}>
                  <label>
                    <span className="sr-only">
                      {day.weekday.toLowerCase()} shift {shiftIndex + 1} start
                    </span>
                    <input
                      required
                      name={`${day.weekday.toLowerCase()}_shift_${shiftIndex + 1}_start`}
                      type="time"
                      className="input"
                      value={shift.start}
                      onChange={(event) =>
                        updateDay(index, {
                          ...day,
                          shifts: day.shifts.map((item, itemIndex) =>
                            itemIndex === shiftIndex
                              ? { ...item, start: event.target.value }
                              : item,
                          ),
                        })
                      }
                    />
                  </label>
                  <span aria-hidden="true">to</span>
                  <label>
                    <span className="sr-only">
                      {day.weekday.toLowerCase()} shift {shiftIndex + 1} end
                    </span>
                    <input
                      required
                      name={`${day.weekday.toLowerCase()}_shift_${shiftIndex + 1}_end`}
                      type="time"
                      className="input"
                      value={shift.end}
                      onChange={(event) =>
                        updateDay(index, {
                          ...day,
                          shifts: day.shifts.map((item, itemIndex) =>
                            itemIndex === shiftIndex
                              ? { ...item, end: event.target.value }
                              : item,
                          ),
                        })
                      }
                    />
                  </label>
                  {day.shifts.length > 1 && (
                    <button
                      type="button"
                      className="icon-button danger"
                      aria-label={`Remove ${day.weekday.toLowerCase()} shift ${shiftIndex + 1}`}
                      onClick={() =>
                        updateDay(index, {
                          ...day,
                          shifts: day.shifts.filter(
                            (_, itemIndex) => itemIndex !== shiftIndex,
                          ),
                        })
                      }
                    >
                      <Trash2 size={16} />
                    </button>
                  )}
                </div>
              ))}
              {day.shifts.length < 3 && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() =>
                    updateDay(index, {
                      ...day,
                      shifts: [...day.shifts, { start: "18:00", end: "20:00" }],
                    })
                  }
                >
                  <Plus size={16} />
                  Add shift
                </button>
              )}
            </div>
          )}
        </fieldset>
      ))}
    </div>
  );
}

const stepTitles = [
  "Basic information",
  "Location",
  "Working hours",
  "Business language",
  "Review and confirm",
];

export function OnboardingPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [business, setBusiness] = useState<Business | null>(null);
  const [draft, setDraft] = useState<BusinessDraft | null>(null);
  const [step, setStep] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .business(id)
      .then((item) => {
        setBusiness(item);
        setDraft(toDraft(item));
        const resume = { business_details: 0, location: 1, working_hours: 2 }[
          item.first_incomplete_section ?? ""
        ];
        setStep(resume === undefined ? (item.profile_complete ? 4 : -1) : resume);
      })
      .catch((caught) => setError(errorMessage(caught)));
  }, [id]);

  if (!business || !draft)
    return (
      <main className="onboarding-page">
        <section className="onboarding-card">
          <Skeleton className="h-96" />
        </section>
      </main>
    );
  const update = (
    key: keyof BusinessDraft,
    value: BusinessDraft[keyof BusinessDraft],
  ) => setDraft((current) => (current ? { ...current, [key]: value } : current));
  const districts = LOCATIONS[draft.governorate] ?? {};
  const cities = districts[draft.district] ?? [];

  async function saveStep(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft) return;
    const currentDraft = draft;
    const submitter = (event.nativeEvent as SubmitEvent)
      .submitter as HTMLButtonElement | null;
    setBusy(true);
    setError("");
    const payload =
      step === 0
        ? {
            name: currentDraft.name,
            category: currentDraft.category,
            custom_category:
              currentDraft.category === "OTHER" ? currentDraft.custom_category : null,
          }
        : step === 1
          ? {
              governorate: currentDraft.governorate,
              district: currentDraft.district,
              city: currentDraft.city,
              address_line: currentDraft.address_line,
            }
          : step === 2
            ? { working_hours: currentDraft.working_hours }
            : {
                default_language: currentDraft.default_language,
                description: currentDraft.description,
              };
    try {
      const saved = await api.updateBusiness(id, payload);
      setBusiness(saved);
      if (submitter?.value === "exit") navigate("/businesses");
      else setStep((current) => Math.min(4, current + 1));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true);
    setError("");
    try {
      await api.confirm(id);
      navigate("/businesses", { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
      setBusy(false);
    }
  }

  return (
    <main className="onboarding-page">
      <div className="auth-wave" aria-hidden="true" />
      <section className="onboarding-card">
        <header className="onboarding-header">
          <Logo />
          <button
            className="icon-button"
            type="button"
            aria-label="Exit onboarding"
            onClick={() => navigate("/businesses")}
          >
            ×
          </button>
        </header>
        {step === -1 ? (
          <div className="onboarding-intro">
            <span className="onboarding-hero-icon">
              <Building2 />
            </span>
            <p className="eyebrow">Business onboarding</p>
            <h1>Create your business</h1>
            <p>
              Set up a complete, accurate profile so Sou2AI can use only the information
              you approve.
            </p>
            <ul>
              <li>
                <Check />
                Centralize your business profile
              </li>
              <li>
                <Check />
                Set exact locations and hours
              </li>
              <li>
                <Check />
                Review everything before submission
              </li>
            </ul>
            <button type="button" className="btn w-full" onClick={() => setStep(0)}>
              Let&apos;s get started <ArrowRight size={18} />
            </button>
          </div>
        ) : (
          <>
            <ol className="stepper" aria-label="Onboarding progress">
              {stepTitles.map((title, index) => (
                <li
                  key={title}
                  className={index <= step ? "active" : ""}
                  aria-current={index === step ? "step" : undefined}
                >
                  <span>{index < step ? <Check size={14} /> : index + 1}</span>
                  <em>{title}</em>
                </li>
              ))}
            </ol>
            <div className="onboarding-title">
              <p>Step {step + 1} of 5</p>
              <h1>{stepTitles[step]}</h1>
              <span>
                {step === 0
                  ? "Tell us the basics about your business."
                  : step === 1
                    ? "Choose an approved Lebanese location."
                    : step === 2
                      ? "Set up to three same-day shifts for each open day."
                      : step === 3
                        ? "Choose a language and describe your business."
                        : "Review the information that will be submitted."}
              </span>
            </div>
            {error && (
              <div className="mb-5">
                <Alert>{error}</Alert>
              </div>
            )}
            {step === 4 ? (
              <div className="review-panel">
                <ReviewRow label="Business name" value={draft.name} />
                <ReviewRow
                  label="Category"
                  value={
                    draft.category === "OTHER"
                      ? draft.custom_category
                      : categoryLabel(draft.category)
                  }
                />
                <ReviewRow
                  label="Location"
                  value={[
                    draft.address_line,
                    draft.city,
                    draft.district,
                    draft.governorate,
                  ]
                    .filter(Boolean)
                    .join(", ")}
                />
                <ReviewRow
                  label="Working hours"
                  value={`${draft.working_hours.filter((day) => !day.is_closed).length} open days per week`}
                />
                <ReviewRow
                  label="Language"
                  value={draft.default_language === "ar" ? "Arabic" : "English"}
                />
                <ReviewRow label="Description" value={draft.description} />
                <Alert tone="info">
                  Confirmation submits this business for review. Its lifecycle status
                  will remain pending.
                </Alert>
                <div className="form-actions">
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => setStep(3)}
                  >
                    <ArrowLeft size={18} />
                    Back
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    className="btn"
                    onClick={() => void confirm()}
                  >
                    <BusyLabel busy={busy} idle="Confirm business" />
                  </button>
                </div>
              </div>
            ) : (
              <form onSubmit={saveStep} className="form-stack">
                {step === 0 && (
                  <>
                    <label className="field-label">
                      Business name
                      <input
                        required
                        name="name"
                        minLength={2}
                        maxLength={120}
                        className="input"
                        value={draft.name}
                        onChange={(event) => update("name", event.target.value)}
                      />
                    </label>
                    <label className="field-label">
                      Business category
                      <select
                        required
                        name="category"
                        className="input"
                        value={draft.category}
                        onChange={(event) => update("category", event.target.value)}
                      >
                        <option value="">Select a category</option>
                        {CATEGORIES.map(([value, label]) => (
                          <option key={value} value={value}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                    {draft.category === "OTHER" && (
                      <label className="field-label">
                        Custom category
                        <input
                          required
                          name="custom_category"
                          minLength={2}
                          maxLength={100}
                          className="input"
                          value={draft.custom_category}
                          onChange={(event) =>
                            update("custom_category", event.target.value)
                          }
                        />
                      </label>
                    )}
                  </>
                )}
                {step === 1 && (
                  <div className="grid gap-4 sm:grid-cols-2">
                    <label className="field-label">
                      Governorate
                      <select
                        required
                        name="governorate"
                        className="input"
                        value={draft.governorate}
                        onChange={(event) => {
                          update("governorate", event.target.value);
                          setDraft((current) =>
                            current
                              ? {
                                  ...current,
                                  governorate: event.target.value,
                                  district: "",
                                  city: "",
                                }
                              : current,
                          );
                        }}
                      >
                        <option value="">Select governorate</option>
                        {Object.keys(LOCATIONS).map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                    <label className="field-label">
                      District
                      <select
                        required
                        name="district"
                        disabled={!draft.governorate}
                        className="input"
                        value={draft.district}
                        onChange={(event) =>
                          setDraft((current) =>
                            current
                              ? { ...current, district: event.target.value, city: "" }
                              : current,
                          )
                        }
                      >
                        <option value="">Select district</option>
                        {Object.keys(districts).map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                    <label className="field-label">
                      City or area
                      <select
                        required
                        name="city"
                        disabled={!draft.district}
                        className="input"
                        value={draft.city}
                        onChange={(event) => update("city", event.target.value)}
                      >
                        <option value="">Select city or area</option>
                        {cities.map((value) => (
                          <option key={value}>{value}</option>
                        ))}
                      </select>
                    </label>
                    <label className="field-label">
                      Street address
                      <input
                        required
                        name="address_line"
                        minLength={5}
                        maxLength={255}
                        className="input"
                        value={draft.address_line}
                        onChange={(event) => update("address_line", event.target.value)}
                      />
                    </label>
                  </div>
                )}
                {step === 2 && (
                  <ScheduleEditor
                    value={draft.working_hours}
                    onChange={(value) => update("working_hours", value)}
                  />
                )}
                {step === 3 && (
                  <>
                    <label className="field-label">
                      Primary language
                      <select
                        required
                        name="default_language"
                        className="input"
                        value={draft.default_language}
                        onChange={(event) =>
                          update("default_language", event.target.value)
                        }
                      >
                        <option value="">Select a language</option>
                        <option value="en">English</option>
                        <option value="ar">Arabic</option>
                      </select>
                    </label>
                    <label className="field-label">
                      About your business
                      <textarea
                        required
                        name="description"
                        minLength={20}
                        maxLength={2000}
                        className="input min-h-36 resize-y"
                        value={draft.description}
                        onChange={(event) => update("description", event.target.value)}
                      />
                      <span className="field-help">
                        {draft.description.length} / 2,000 characters
                      </span>
                    </label>
                  </>
                )}
                <div className="form-actions">
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => setStep((current) => Math.max(-1, current - 1))}
                  >
                    <ArrowLeft size={18} />
                    Back
                  </button>
                  <div className="flex flex-wrap gap-2">
                    <button type="submit" value="exit" className="btn-secondary">
                      Save and exit
                    </button>
                    <button disabled={busy} type="submit" value="next" className="btn">
                      <BusyLabel busy={busy} idle="Next" />
                      {!busy && <ArrowRight size={18} />}
                    </button>
                  </div>
                </div>
              </form>
            )}
          </>
        )}
      </section>
    </main>
  );
}

function ReviewRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="review-row">
      <span>{label}</span>
      <strong>{value || "Not provided"}</strong>
    </div>
  );
}
