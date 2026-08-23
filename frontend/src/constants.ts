import type { WorkingDay } from "./api";

export const CATEGORIES = [
  ["GROCERY_SUPERMARKET", "Grocery & supermarket"],
  ["BAKERY", "Bakery"],
  ["RESTAURANT", "Restaurant"],
  ["CAFE", "Café"],
  ["PHARMACY", "Pharmacy"],
  ["OTHER", "Other"],
] as const;

export const DAYS = [
  "MONDAY",
  "TUESDAY",
  "WEDNESDAY",
  "THURSDAY",
  "FRIDAY",
  "SATURDAY",
  "SUNDAY",
] as const;

export const LOCATIONS: Record<string, Record<string, string[]>> = {
  Beirut: { Beirut: ["Beirut"] },
  "Mount Lebanon": {
    Baabda: ["Baabda", "Hazmieh"],
    Aley: ["Aley", "Choueifat"],
    Metn: ["Antelias", "Jdeideh", "Sin El Fil", "Dekwaneh", "Baouchrieh"],
    Keserwan: ["Jounieh", "Zouk Mikael", "Kaslik"],
    Chouf: ["Beiteddine", "Damour", "Deir El Qamar"],
  },
  North: {
    Tripoli: ["Tripoli", "Mina"],
    Zgharta: ["Zgharta", "Ehden"],
    Koura: ["Amioun"],
  },
  Akkar: { Akkar: ["Halba"] },
  Bekaa: {
    Zahle: ["Zahle", "Chtaura"],
    "West Bekaa": ["Jeb Jennine", "Qab Elias"],
  },
  "Baalbek-Hermel": { Baalbek: ["Baalbek"], Hermel: ["Hermel"] },
  South: { Saida: ["Saida", "Abra", "Ghaziyeh"], Jezzine: ["Jezzine"] },
  Nabatieh: {
    Nabatieh: ["Nabatieh", "Kfar Roummane"],
    "Bint Jbeil": ["Bint Jbeil"],
    Marjayoun: ["Marjayoun", "Khiam"],
  },
};

export function emptySchedule(): WorkingDay[] {
  return DAYS.map((weekday, index) => ({
    weekday,
    is_closed: index === 6,
    shifts: index === 6 ? [] : [{ start: "09:00", end: "17:00" }],
  }));
}

export function categoryLabel(value: string | null) {
  return CATEGORIES.find(([key]) => key === value)?.[1] ?? "Category not set";
}
