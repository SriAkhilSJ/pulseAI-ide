export function getUserAge(birthdate) {
    return new Date().getFullYear() - new Date(birthdate).getFullYear();
}
